import os
import shutil
import random
import asyncio
import logging
from datetime import datetime, timedelta

import pytz
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler, filters,
    ContextTypes, PicklePersistence,
)
from dotenv import load_dotenv

import db
from downloader import detect_platform, download_tiktok, download_likee, download_vk

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
VK_API_VERSION = "5.199"
MOSCOW_TZ = pytz.timezone("Europe/Moscow")

PLATFORM_LABELS = {
    "tiktok": "TikTok",
    "likee": "Likee",
    "vk": "VK",
}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── Состояния разговоров ─────────────────────────────────────────────────────

# Загрузка видео
UP_GROUP, UP_DESC, UP_CUSTOM_DESC, UP_TIME, UP_CUSTOM_TIME = range(5)
# Установка токена
TOKEN_WAIT = 10
# Управление группами
G_ADD_ID, G_ADD_CONFIRM, G_ADD_NAME, G_RENAME = range(20, 24)
# Управление заготовками
T_TITLE, T_BODY = range(30, 32)

TIME_SLOTS = [9, 15, 20]

# ─── Контроль нагрузки на VK (настраивается через .env) ───────────────────────
# Сколько публикаций может уходить в VK одновременно. Несколько роликов,
# запланированных на один слот, стартуют почти одновременно — семафор
# выстраивает их в очередь, чтобы не словить VK rate limit (error_code 6/9).
VK_PUBLISH_CONCURRENCY = int(os.getenv("VK_PUBLISH_CONCURRENCY", "1"))
_vk_publish_semaphore = asyncio.Semaphore(VK_PUBLISH_CONCURRENCY)

# Ретрай публикации при временных ошибках VK / сети.
VK_PUBLISH_RETRIES = int(os.getenv("VK_PUBLISH_RETRIES", "3"))       # всего попыток
VK_RETRY_BASE_DELAY = float(os.getenv("VK_RETRY_BASE_DELAY", "3"))   # секунды, растёт экспоненциально
# Коды ошибок VK, при которых имеет смысл повторить запрос.
VK_RETRYABLE_ERROR_CODES = {1, 6, 9, 10}  # неизвестная/too many/flood/internal

# Джиттер времени публикации: чтобы ролики не выходили ровно в HH:00:00
# (для реков — «живее», когда время чуть «плавает»).
PUBLISH_JITTER_SECONDS = int(os.getenv("PUBLISH_JITTER_SECONDS", "300"))

# Активные задачи загрузки по message_id статусного сообщения
# (Task не сериализуется, поэтому не в user_data; ключ по message_id —
#  чтобы поддержать несколько параллельных загрузок и точечную отмену каждой).
_upload_tasks: dict[int, asyncio.Task] = {}


class VKError(RuntimeError):
    """Ошибка VK API с кодом — чтобы отличать временные сбои от фатальных."""

    def __init__(self, code: int | None, message: str):
        self.code = code
        super().__init__(message)

CANCEL_MARKUP = InlineKeyboardMarkup([[
    InlineKeyboardButton("❌ Отменить", callback_data="cancel_upload")
]])

# ─── Постоянное меню (кнопки над клавиатурой) ─────────────────────────────────
BTN_TOKEN = "🔑 Мой токен"
BTN_GROUPS = "👥 Группы"
BTN_TEMPLATES = "📝 Описания"
MENU_BUTTON_TEXTS = [BTN_TOKEN, BTN_GROUPS, BTN_TEMPLATES]


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[BTN_TOKEN, BTN_GROUPS, BTN_TEMPLATES]],
        resize_keyboard=True,
    )


# ─── Helpers ────────────────────────────────────────────────────────────────

def parse_group_id(text: str) -> int | None:
    """Извлекает числовой ID группы из ввода (минус и URL игнорируем)."""
    text = text.strip().rstrip("/")
    if "/" in text:
        text = text.rsplit("/", 1)[-1]
    text = text.lstrip("-")
    digits = "".join(ch for ch in text if ch.isdigit())
    return int(digits) if digits else None


def fetch_vk_group_name(vk_token: str, group_id: int) -> str | None:
    """Пробует получить название группы через VK API. None — если не удалось."""
    try:
        resp = requests.get(
            "https://api.vk.com/method/groups.getById",
            params={
                "access_token": vk_token,
                "v": VK_API_VERSION,
                "group_id": group_id,
            },
            timeout=15,
        ).json()
    except Exception:
        logger.exception("Ошибка запроса groups.getById")
        return None

    if "error" in resp:
        logger.info("groups.getById error: %s", resp["error"])
        return None

    response = resp.get("response")
    try:
        if isinstance(response, list):
            return response[0]["name"]
        if isinstance(response, dict):
            return response["groups"][0]["name"]
    except (KeyError, IndexError, TypeError):
        pass
    return None


# Папка для хранения скачанных файлов до момента публикации.
# Файлы лежат здесь от момента скачивания до запланированного времени —
# могут пережить перезапуск бота (volume в Docker смонтирован).
PENDING_DIR = os.path.join(db.DATA_DIR, "pending_videos")


def upload_to_vk(
    vk_token: str,
    vk_group_id: int,
    file_path: str,
    title: str,
    description: str,
) -> None:
    """Загружает видео в VK и сразу публикует запись на стене группы.

    Всегда публикует НЕМЕДЛЕННО — планирование времени делается на стороне
    бота (job_queue), а не через publish_date в VK API. Это гарантирует, что
    видео не появится в разделе «Видео» группы раньше времени.
    """
    group_id = abs(int(vk_group_id))
    logger.info("upload_to_vk: group_id=%s description=%r", group_id, description)

    save_data = {
        "access_token": vk_token,
        "v": VK_API_VERSION,
        "group_id": group_id,
        "name": title,
        "wallpost": 0,
    }
    if description:
        save_data["description"] = description

    save_resp = requests.post(
        "https://api.vk.com/method/video.save",
        data=save_data,
        timeout=30,
    ).json()
    logger.info("video.save response: %s", save_resp)

    if "error" in save_resp:
        e = save_resp["error"]
        raise VKError(e.get("error_code"), f"VK video.save ошибка {e.get('error_code')}: {e.get('error_msg')}")

    video_id = save_resp["response"]["video_id"]
    owner_id = save_resp["response"]["owner_id"]
    upload_url = save_resp["response"]["upload_url"]

    with open(file_path, "rb") as f:
        upload_resp = requests.post(upload_url, files={"video_file": f}, timeout=300)
        upload_resp.raise_for_status()
        logger.info("video upload response: %s", upload_resp.text[:500])

    wall_params = {
        "access_token": vk_token,
        "v": VK_API_VERSION,
        "owner_id": f"-{group_id}",
        "message": description,
        "attachments": f"video{owner_id}_{video_id}",
        "from_group": 1,
    }
    wall_resp = requests.post(
        "https://api.vk.com/method/wall.post", data=wall_params, timeout=30
    ).json()
    logger.info("wall.post response: %s", wall_resp)

    if "error" in wall_resp:
        e = wall_resp["error"]
        try:
            requests.post(
                "https://api.vk.com/method/video.delete",
                data={
                    "access_token": vk_token,
                    "v": VK_API_VERSION,
                    "owner_id": owner_id,
                    "video_id": video_id,
                },
                timeout=30,
            )
        except Exception:
            logger.exception("Не удалось откатить видео")
        raise VKError(
            e.get("error_code"),
            f"VK wall.post ошибка {e.get('error_code')}: {e.get('error_msg')}",
        )


async def _publish_to_vk(
    vk_token: str,
    vk_group_id: int,
    file_path: str,
    title: str,
    description: str,
) -> None:
    """Публикует видео в VK с ограничением одновременности и ретраями.

    - семафор (1): запросы к VK не идут лавиной, даже если в один слот попало
      много роликов — они выстраиваются в очередь;
    - ретрай с экспоненциальным backoff на временные ошибки VK (rate limit /
      flood / internal) и сетевые сбои.
    """
    loop = asyncio.get_running_loop()
    async with _vk_publish_semaphore:
        last_exc: Exception | None = None
        for attempt in range(1, VK_PUBLISH_RETRIES + 1):
            try:
                await loop.run_in_executor(
                    None,
                    lambda: upload_to_vk(vk_token, vk_group_id, file_path, title, description),
                )
                return
            except VKError as exc:
                last_exc = exc
                if exc.code not in VK_RETRYABLE_ERROR_CODES or attempt == VK_PUBLISH_RETRIES:
                    raise
            except requests.exceptions.RequestException as exc:
                last_exc = exc
                if attempt == VK_PUBLISH_RETRIES:
                    raise

            delay = VK_RETRY_BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 2)
            logger.warning(
                "Публикация в VK не удалась (попытка %s/%s): %s. Повтор через %.1f c",
                attempt, VK_PUBLISH_RETRIES, last_exc, delay,
            )
            await asyncio.sleep(delay)


async def _scheduled_upload_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """PTB job: вызывается в момент запланированной публикации.

    Загружает видео в VK и сразу публикует — никаких publish_date не передаём,
    поэтому видео появляется в группе ровно в этот момент и ровно один раз.
    """
    data = context.job.data
    chat_id = data["chat_id"]
    file_path = data["file_path"]
    group_name = data.get("vk_group_name") or "VK"

    try:
        if not os.path.exists(file_path):
            await context.bot.send_message(
                chat_id,
                "❌ Не удалось опубликовать: файл видео не найден.\n"
                "Возможно, бот перезапускался и временный файл был удалён. Загрузи видео заново."
            )
            return

        await context.bot.send_message(chat_id, "⏰ Публикую видео по расписанию...")

        await _publish_to_vk(
            data["vk_token"],
            data["vk_group_id"],
            file_path,
            data["title"],
            data["description"],
        )

        await context.bot.send_message(chat_id, f"✅ Видео опубликовано в {group_name}!")
    except Exception as exc:
        logger.exception("Ошибка отложенной публикации chat_id=%s", chat_id)
        try:
            await context.bot.send_message(chat_id, f"❌ Ошибка публикации: {exc}")
        except Exception:
            pass
    finally:
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except OSError:
                pass


# ─── Keyboards ────────────────────────────────────────────────────────────────

def build_time_keyboard() -> InlineKeyboardMarkup:
    now = datetime.now(MOSCOW_TZ)
    today = now.date()
    tomorrow = today + timedelta(days=1)

    keyboard = [[InlineKeyboardButton("⚡ Сейчас", callback_data="now")]]

    today_btns = []
    for h in TIME_SLOTS:
        slot = MOSCOW_TZ.localize(datetime(today.year, today.month, today.day, h))
        if slot > now + timedelta(minutes=5):
            today_btns.append(
                InlineKeyboardButton(f"Сегодня {h}:00", callback_data=f"slot_{today.isoformat()}_{h}")
            )
    if today_btns:
        keyboard.append(today_btns)

    keyboard.append([
        InlineKeyboardButton(f"Завтра {h}:00", callback_data=f"slot_{tomorrow.isoformat()}_{h}")
        for h in TIME_SLOTS
    ])
    keyboard.append([InlineKeyboardButton("✏️ Своё время", callback_data="custom")])
    return InlineKeyboardMarkup(keyboard)


def build_groups_select_keyboard(telegram_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(g["name"], callback_data=f"upgroup_{g['id']}")]
        for g in db.get_groups(telegram_id)
    ]
    return InlineKeyboardMarkup(rows)


def build_desc_keyboard(telegram_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"📝 {t['title']}", callback_data=f"updesc_tpl_{t['id']}")]
        for t in db.get_templates(telegram_id)
    ]
    rows.append([InlineKeyboardButton("✏️ Написать своё", callback_data="updesc_custom")])
    rows.append([InlineKeyboardButton("➖ Без описания", callback_data="updesc_none")])
    return InlineKeyboardMarkup(rows)


def build_groups_manage_keyboard(telegram_id: int) -> InlineKeyboardMarkup:
    rows = []
    for g in db.get_groups(telegram_id):
        rows.append([InlineKeyboardButton(f"{g['name']} (id {g['vk_group_id']})", callback_data="noop")])
        rows.append([
            InlineKeyboardButton("✏️ Переименовать", callback_data=f"g_rename_{g['id']}"),
            InlineKeyboardButton("🗑 Удалить", callback_data=f"g_del_{g['id']}"),
        ])
    rows.append([InlineKeyboardButton("➕ Добавить группу", callback_data="g_add")])
    return InlineKeyboardMarkup(rows)


def build_token_keyboard(has_token: bool) -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton(
            "✏️ Изменить токен" if has_token else "➕ Задать токен",
            callback_data="settoken_change",
        )
    ]]
    if has_token:
        rows.append([InlineKeyboardButton("🗑 Удалить токен", callback_data="settoken_delete")])
    return InlineKeyboardMarkup(rows)


def _mask_token(token: str) -> str:
    if len(token) <= 12:
        return "•" * len(token)
    return f"{token[:6]}…{token[-4:]}"


def build_templates_manage_keyboard(telegram_id: int) -> InlineKeyboardMarkup:
    rows = []
    for t in db.get_templates(telegram_id):
        rows.append([InlineKeyboardButton(f"📝 {t['title']}", callback_data="noop")])
        rows.append([
            InlineKeyboardButton("✏️ Изменить", callback_data=f"t_edit_{t['id']}"),
            InlineKeyboardButton("🗑 Удалить", callback_data=f"t_del_{t['id']}"),
        ])
    rows.append([InlineKeyboardButton("➕ Добавить заготовку", callback_data="t_add")])
    return InlineKeyboardMarkup(rows)


# ─── Core upload flow ────────────────────────────────────────────────────────

async def do_upload(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    job: dict,
    publish_date: int | None = None,
    status_message=None,
    task_key: int | None = None,
):
    # job — снимок данных на момент старта. context.user_data НЕ используем:
    # пользователь может начать новый поток, и общий user_data будет перезаписан.
    url = job["url"]
    platform = job["platform"]
    description = job.get("description", "")
    vk_token = job["vk_token"]
    vk_group_id = job["vk_group_id"]
    vk_group_name = job.get("vk_group_name") or "VK"
    file_path: str | None = None

    async def set_status(text: str, final: bool = False):
        markup = None if final else CANCEL_MARKUP
        if status_message:
            await status_message.edit_text(text, reply_markup=markup)
        else:
            await context.bot.send_message(chat_id, text, reply_markup=markup)

    try:
        await set_status(f"⏳ Скачиваю видео с {PLATFORM_LABELS.get(platform, platform)}...")

        if platform == "tiktok":
            file_path, title = await download_tiktok(url, None)
        elif platform == "likee":
            file_path, title = await download_likee(url)
        elif platform == "vk":
            file_path, title = await download_vk(url, vk_token)
        else:
            raise ValueError(f"Неизвестная платформа: {platform}")

        logger.info("do_upload: description=%r publish_date=%s", description, publish_date)

        if publish_date:
            # Скачали — теперь перекладываем в постоянное хранилище и
            # ставим задачу на нужное время. В VK ничего не грузим до этого
            # момента — иначе видео сразу появится в разделе «Видео» группы.
            os.makedirs(PENDING_DIR, exist_ok=True)
            persistent_path = os.path.join(PENDING_DIR, os.path.basename(file_path))
            shutil.move(file_path, persistent_path)
            file_path = None  # файл перемещён, finally не должен его удалять

            # Джиттер: сдвигаем фактическую публикацию на случайные секунды
            # вперёд, чтобы ролики не выходили ровно в HH:00:00 — для реков
            # «живее», и заодно разносит во времени видео из одного слота.
            jitter = random.randint(0, PUBLISH_JITTER_SECONDS)
            dt = datetime.fromtimestamp(publish_date + jitter, tz=MOSCOW_TZ)
            context.job_queue.run_once(
                _scheduled_upload_job,
                when=dt,
                data={
                    "chat_id": chat_id,
                    "file_path": persistent_path,
                    "title": title,
                    "description": description,
                    "vk_token": vk_token,
                    "vk_group_id": vk_group_id,
                    "vk_group_name": vk_group_name,
                },
                name=f"scheduled_{task_key}",
            )

            await set_status(
                f"✅ Видео скачано!\n\n"
                f"📅 Опубликую примерно {dt.strftime('%d.%m.%Y в %H:%M')} МСК "
                f"в «{vk_group_name}».",
                final=True,
            )
        else:
            size_mb = os.path.getsize(file_path) / (1024 * 1024)
            await set_status(f"📤 Загружаю в VK...\nРазмер: {size_mb:.1f} МБ")
            await _publish_to_vk(vk_token, vk_group_id, file_path, title, description)
            await set_status(f"✅ Опубликовано в {vk_group_name}!", final=True)

    except asyncio.CancelledError:
        try:
            if status_message:
                await status_message.edit_text("❌ Загрузка отменена")
            else:
                await context.bot.send_message(chat_id, "❌ Загрузка отменена")
        except Exception:
            pass
        raise
    except Exception as exc:
        logger.exception("Ошибка обработки %s", url)
        await set_status(f"❌ Ошибка\n\n{exc}", final=True)
    finally:
        if task_key is not None:
            _upload_tasks.pop(task_key, None)
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except OSError:
                pass


def _snapshot_job(context: ContextTypes.DEFAULT_TYPE) -> dict:
    """Фиксирует данные текущего потока, чтобы фоновая загрузка не зависела от
    последующих изменений context.user_data (новый поток / параллельная загрузка)."""
    return {
        "url": context.user_data["url"],
        "platform": context.user_data["platform"],
        "description": context.user_data.get("description", ""),
        "vk_token": context.user_data["vk_token"],
        "vk_group_id": context.user_data["vk_group_id"],
        "vk_group_name": context.user_data.get("vk_group_name", ""),
    }


def _start_upload(chat_id: int, context: ContextTypes.DEFAULT_TYPE, publish_date: int | None, status_msg) -> None:
    """Запускает загрузку в фоне и СРАЗУ возвращается — диспетчер бота не блокируется,
    поэтому бот продолжает отвечать на другие сообщения во время скачивания/заливки."""
    job = _snapshot_job(context)
    task_key = status_msg.message_id
    task = asyncio.create_task(
        do_upload(
            chat_id, context, job,
            publish_date=publish_date,
            status_message=status_msg,
            task_key=task_key,
        )
    )
    _upload_tasks[task_key] = task


# ─── /start ───────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db.ensure_user(update.effective_user.id)
    await update.message.reply_text(
        "Привет! Я скачиваю видео из TikTok, Likee и VK и публикую в твою группу VK.\n\n"
        "Кнопки внизу:\n"
        f"{BTN_TOKEN} — посмотреть / изменить / удалить VK токен\n"
        f"{BTN_GROUPS} — управление группами VK\n"
        f"{BTN_TEMPLATES} — заготовки описаний\n\n"
        "Чтобы опубликовать видео — просто пришли ссылку на TikTok, Likee или VK.",
        reply_markup=main_keyboard(),
    )


# ─── Upload conversation ────────────────────────────────────────────────────

async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    url = update.message.text.strip()
    platform = detect_platform(url)
    if not platform:
        await update.message.reply_text(
            "Не распознал ссылку. Поддерживаются:\n"
            "• TikTok (tiktok.com)\n"
            "• Likee (likee.video)\n"
            "• VK видео и клипы (vk.com/video…, vk.com/clip…)"
        )
        return ConversationHandler.END

    telegram_id = update.effective_user.id
    db.ensure_user(telegram_id)

    vk_token = db.get_vk_token(telegram_id)
    if not vk_token:
        await update.message.reply_text("Сначала задай VK токен командой /settoken")
        return ConversationHandler.END

    groups = db.get_groups(telegram_id)
    if not groups:
        await update.message.reply_text("Сначала добавь хотя бы одну группу командой /groups")
        return ConversationHandler.END

    context.user_data["url"] = url
    context.user_data["platform"] = platform
    context.user_data["vk_token"] = vk_token
    await update.message.reply_text(
        f"Ссылка {PLATFORM_LABELS[platform]} принята.\nВ какую группу опубликовать?",
        reply_markup=build_groups_select_keyboard(telegram_id),
    )
    return UP_GROUP


async def handle_group_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    group_row_id = int(query.data.split("_")[1])
    group = db.get_group(group_row_id)
    if not group:
        await query.edit_message_text("Группа не найдена. Начни заново — пришли ссылку.")
        return ConversationHandler.END

    context.user_data["vk_group_id"] = group["vk_group_id"]
    context.user_data["vk_group_name"] = group["name"]
    telegram_id = update.effective_user.id
    await query.edit_message_text(
        f"Группа: {group['name']}\n\nВыбери описание:",
        reply_markup=build_desc_keyboard(telegram_id),
    )
    return UP_DESC


async def handle_desc_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "updesc_custom":
        await query.edit_message_text("Введи текст описания для публикации:")
        return UP_CUSTOM_DESC

    if data == "updesc_none":
        context.user_data["description"] = ""
    else:  # updesc_tpl_<id>
        template_id = int(data.rsplit("_", 1)[1])
        template = db.get_template(template_id)
        context.user_data["description"] = template["body"] if template else ""

    await query.edit_message_text("Когда опубликовать видео?", reply_markup=build_time_keyboard())
    return UP_TIME


async def handle_custom_desc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["description"] = update.message.text
    await update.message.reply_text("Когда опубликовать видео?", reply_markup=build_time_keyboard())
    return UP_TIME


async def handle_time_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = query.message.chat_id

    if data == "now":
        status_msg = await query.edit_message_text("⏳ Начинаю...", reply_markup=CANCEL_MARKUP)
        _start_upload(chat_id, context, publish_date=None, status_msg=status_msg)
        return ConversationHandler.END

    if data == "custom":
        await query.edit_message_text(
            "Введи дату и время в формате ДД.ММ.ГГГГ ЧЧ:ММ\nНапример: 25.12.2024 18:30 (время московское)"
        )
        return UP_CUSTOM_TIME

    # slot_YYYY-MM-DD_HH
    _, date_str, hour_str = data.split("_")
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    h = int(hour_str)
    scheduled_time = MOSCOW_TZ.localize(datetime(d.year, d.month, d.day, h))
    status_msg = await query.edit_message_text("⏳ Начинаю...", reply_markup=CANCEL_MARKUP)
    _start_upload(chat_id, context, publish_date=int(scheduled_time.timestamp()), status_msg=status_msg)
    return ConversationHandler.END


async def handle_custom_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    try:
        scheduled_time = MOSCOW_TZ.localize(datetime.strptime(text, "%d.%m.%Y %H:%M"))
    except ValueError:
        await update.message.reply_text(
            "Неверный формат. Введи так: ДД.ММ.ГГГГ ЧЧ:ММ\nНапример: 25.12.2024 18:30"
        )
        return UP_CUSTOM_TIME

    if scheduled_time <= datetime.now(MOSCOW_TZ) + timedelta(minutes=1):
        await update.message.reply_text("Это время уже прошло. Введи время в будущем:")
        return UP_CUSTOM_TIME

    status_msg = await update.message.reply_text("⏳ Начинаю...", reply_markup=CANCEL_MARKUP)
    _start_upload(update.message.chat_id, context, publish_date=int(scheduled_time.timestamp()), status_msg=status_msg)
    return ConversationHandler.END


async def handle_cancel_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer("Отмена...")
    # Кнопка «Отмена» висит на том же сообщении, по которому задача и ключуется.
    task = _upload_tasks.get(query.message.message_id)
    if task and not task.done():
        task.cancel()
    else:
        await query.edit_message_text("Нечего отменять")


# ─── Меню (кнопки над клавиатурой) ────────────────────────────────────────────

async def _reply_groups(update: Update, telegram_id: int) -> None:
    await update.message.reply_text(
        "Твои группы VK:",
        reply_markup=build_groups_manage_keyboard(telegram_id),
    )


async def _reply_templates(update: Update, telegram_id: int) -> None:
    await update.message.reply_text(
        "Твои заготовки описаний:",
        reply_markup=build_templates_manage_keyboard(telegram_id),
    )


async def main_menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text
    telegram_id = update.effective_user.id
    db.ensure_user(telegram_id)

    if text == BTN_GROUPS:
        await _reply_groups(update, telegram_id)
    elif text == BTN_TEMPLATES:
        await _reply_templates(update, telegram_id)
    elif text == BTN_TOKEN:
        await show_token_status(update, context)


async def show_token_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    token = db.get_vk_token(update.effective_user.id)
    if token:
        msg = f"🔑 Токен задан: {_mask_token(token)}\n(показан частично — в целях безопасности)"
    else:
        msg = "❌ Токен не задан."
    await update.message.reply_text(msg, reply_markup=build_token_keyboard(bool(token)))


async def handle_token_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer("Токен удалён")
    db.clear_vk_token(update.effective_user.id)
    await query.edit_message_text("🗑 Токен удалён.", reply_markup=build_token_keyboard(False))


# ─── /settoken conversation ───────────────────────────────────────────────────

SETTOKEN_PROMPT = (
    "Пришли свой VK токен.\n\n"
    "Как получить через Kate Mobile:\n"
    "1. Открой браузер и перейди по ссылке:\n"
    "https://oauth.vk.com/authorize?client_id=2685278&scope=1073737727&redirect_uri=https://oauth.vk.com/blank.html&display=page&response_type=token\n"
    "2. Войди в VK и разреши доступ\n"
    "3. Скопируй access_token из адресной строки (между access_token= и &expires_in)"
)


async def cmd_settoken(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(SETTOKEN_PROMPT)
    return TOKEN_WAIT


async def settoken_from_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(SETTOKEN_PROMPT)
    return TOKEN_WAIT


async def handle_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    token = update.message.text.strip()

    # Пользователь, видимо, передумал вводить токен и прислал ссылку/нажал кнопку меню —
    # не сохраняем это как токен (фикс бага, когда ссылка попадала в токен).
    if detect_platform(token) or token in MENU_BUTTON_TEXTS:
        await update.message.reply_text(
            "Похоже, это не VK токен — ввод токена отменён.\n"
            f"Если хотел задать токен, нажми «{BTN_TOKEN}» и пришли его."
        )
        return ConversationHandler.END

    # Новый формат VK токена: vk1.a.XXXX (минимум 20 символов после префикса)
    # Старый формат: длинная строка без пробелов (85+ символов)
    is_new = token.startswith("vk1.a.") and len(token) >= 26
    is_old = len(token) >= 85 and not any(ch.isspace() for ch in token)
    if not (is_new or is_old):
        await update.message.reply_text(
            "❌ Это не похоже на VK токен.\n\n"
            "VK токен выглядит так:\n"
            "<code>vk1.a.AbCdEfGhIj...</code>\n\n"
            "Пришли правильный токен или нажми /cancel.",
            parse_mode="HTML",
        )
        return TOKEN_WAIT

    db.set_vk_token(update.effective_user.id, token)
    await update.message.reply_text("✅ Токен сохранён.", reply_markup=main_keyboard())
    return ConversationHandler.END


# ─── /groups conversation ─────────────────────────────────────────────────────

async def cmd_groups(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    telegram_id = update.effective_user.id
    db.ensure_user(telegram_id)
    await _reply_groups(update, telegram_id)
    return ConversationHandler.END


async def groups_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    data = query.data

    if data == "noop":
        await query.answer()
        return ConversationHandler.END

    if data == "g_add":
        await query.answer()
        await query.edit_message_text("Пришли ID группы VK (только цифры):")
        return G_ADD_ID

    if data.startswith("g_del_"):
        await query.answer("Удалено")
        db.delete_group(int(data.rsplit("_", 1)[1]))
        await query.edit_message_text(
            "Твои группы VK:",
            reply_markup=build_groups_manage_keyboard(update.effective_user.id),
        )
        return ConversationHandler.END

    if data.startswith("g_rename_"):
        await query.answer()
        context.user_data["rename_group_id"] = int(data.rsplit("_", 1)[1])
        await query.edit_message_text("Введи новое название группы:")
        return G_RENAME

    await query.answer()
    return ConversationHandler.END


async def groups_add_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    group_id = parse_group_id(update.message.text)
    if not group_id:
        await update.message.reply_text("Не похоже на ID. Пришли только цифры, например: 239622117")
        return G_ADD_ID

    context.user_data["pending_group_id"] = group_id
    vk_token = db.get_vk_token(update.effective_user.id)
    if vk_token:
        loop = asyncio.get_running_loop()
        name = await loop.run_in_executor(None, fetch_vk_group_name, vk_token, group_id)
    else:
        name = None

    if name:
        context.user_data["pending_group_name"] = name
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Сохранить", callback_data="g_confirmname")],
            [InlineKeyboardButton("✏️ Задать своё имя", callback_data="g_manualname")],
        ])
        await update.message.reply_text(f"Нашёл группу: «{name}»\nСохранить с этим именем?", reply_markup=keyboard)
        return G_ADD_CONFIRM

    await update.message.reply_text(
        "Не удалось получить название группы автоматически. Введи название вручную:"
    )
    return G_ADD_NAME


async def groups_add_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    telegram_id = update.effective_user.id

    if query.data == "g_confirmname":
        db.add_group(
            telegram_id,
            context.user_data["pending_group_id"],
            context.user_data["pending_group_name"],
        )
        await query.edit_message_text(
            "✅ Группа добавлена.\n\nТвои группы VK:",
            reply_markup=build_groups_manage_keyboard(telegram_id),
        )
        return ConversationHandler.END

    # g_manualname
    await query.edit_message_text("Введи название группы вручную:")
    return G_ADD_NAME


async def groups_add_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    telegram_id = update.effective_user.id
    db.add_group(
        telegram_id,
        context.user_data["pending_group_id"],
        update.message.text.strip(),
    )
    await update.message.reply_text(
        "✅ Группа добавлена.\n\nТвои группы VK:",
        reply_markup=build_groups_manage_keyboard(telegram_id),
    )
    return ConversationHandler.END


async def groups_rename(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    telegram_id = update.effective_user.id
    db.rename_group(context.user_data["rename_group_id"], update.message.text.strip())
    await update.message.reply_text(
        "✅ Переименовано.\n\nТвои группы VK:",
        reply_markup=build_groups_manage_keyboard(telegram_id),
    )
    return ConversationHandler.END


# ─── /templates conversation ──────────────────────────────────────────────────

async def cmd_templates(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    telegram_id = update.effective_user.id
    db.ensure_user(telegram_id)
    await _reply_templates(update, telegram_id)
    return ConversationHandler.END


async def templates_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    data = query.data

    if data == "noop":
        await query.answer()
        return ConversationHandler.END

    if data == "t_add":
        await query.answer()
        context.user_data.pop("edit_template_id", None)
        await query.edit_message_text("Введи название заготовки (короткое, для себя):")
        return T_TITLE

    if data.startswith("t_del_"):
        await query.answer("Удалено")
        db.delete_template(int(data.rsplit("_", 1)[1]))
        await query.edit_message_text(
            "Твои заготовки описаний:",
            reply_markup=build_templates_manage_keyboard(update.effective_user.id),
        )
        return ConversationHandler.END

    if data.startswith("t_edit_"):
        await query.answer()
        template_id = int(data.rsplit("_", 1)[1])
        context.user_data["edit_template_id"] = template_id
        await query.edit_message_text("Введи новое название заготовки:")
        return T_TITLE

    await query.answer()
    return ConversationHandler.END


async def templates_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["template_title"] = update.message.text.strip()
    await update.message.reply_text("Теперь введи текст описания:")
    return T_BODY


async def templates_body(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    telegram_id = update.effective_user.id
    title = context.user_data["template_title"]
    body = update.message.text

    edit_id = context.user_data.get("edit_template_id")
    if edit_id:
        db.update_template(edit_id, title, body)
        msg = "✅ Заготовка обновлена."
    else:
        db.add_template(telegram_id, title, body)
        msg = "✅ Заготовка добавлена."

    await update.message.reply_text(
        f"{msg}\n\nТвои заготовки описаний:",
        reply_markup=build_templates_manage_keyboard(telegram_id),
    )
    return ConversationHandler.END


# ─── Общий /cancel ────────────────────────────────────────────────────────────

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Отменено.")
    return ConversationHandler.END


# ─── Main ────────────────────────────────────────────────────────────────────

def main() -> None:
    if not TELEGRAM_TOKEN:
        raise SystemExit("Не задан TELEGRAM_TOKEN в .env")

    db.init_db()

    persistence = PicklePersistence(
        filepath=os.path.join(db.DATA_DIR, "bot_state.pickle")
    )
    # concurrent_updates=True — апдейты обрабатываются параллельно, поэтому медленная
    # операция в одном потоке не «замораживает» ответы остальным сообщениям.
    # Увеличенные таймауты и пул соединений — чтобы случайные обрывы/медленная
    # сеть до api.telegram.org не валили обработку с TimedOut.
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .persistence(persistence)
        .concurrent_updates(True)
        .connect_timeout(30.0)
        .read_timeout(30.0)
        .write_timeout(30.0)
        .pool_timeout(30.0)
        .get_updates_connect_timeout(30.0)
        .get_updates_read_timeout(30.0)
        .build()
    )

    upload_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link)],
        states={
            UP_GROUP: [CallbackQueryHandler(handle_group_choice, pattern=r"^upgroup_")],
            UP_DESC: [CallbackQueryHandler(handle_desc_choice, pattern=r"^updesc_")],
            UP_CUSTOM_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_custom_desc)],
            UP_TIME: [CallbackQueryHandler(handle_time_choice, pattern=r"^(now|custom|slot_)")],
            UP_CUSTOM_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_custom_time)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        per_message=False,
        conversation_timeout=300,
        name="upload_conv",
        persistent=False,
        allow_reentry=True,
    )

    token_conv = ConversationHandler(
        entry_points=[
            CommandHandler("settoken", cmd_settoken),
            CallbackQueryHandler(settoken_from_button, pattern=r"^settoken_change$"),
        ],
        states={TOKEN_WAIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_token)]},
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        per_message=False,
        conversation_timeout=300,
        name="token_conv",
        persistent=False,
        allow_reentry=True,
    )

    groups_conv = ConversationHandler(
        entry_points=[
            CommandHandler("groups", cmd_groups),
            CallbackQueryHandler(groups_button, pattern=r"^(g_add|g_del_|g_rename_|noop$)"),
        ],
        states={
            G_ADD_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, groups_add_id)],
            G_ADD_CONFIRM: [CallbackQueryHandler(groups_add_confirm, pattern=r"^g_(confirmname|manualname)$")],
            G_ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, groups_add_name)],
            G_RENAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, groups_rename)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        per_message=False,
        conversation_timeout=300,
        name="groups_conv",
        persistent=False,
        allow_reentry=True,
    )

    templates_conv = ConversationHandler(
        entry_points=[
            CommandHandler("templates", cmd_templates),
            CallbackQueryHandler(templates_button, pattern=r"^(t_add|t_del_|t_edit_|noop$)"),
        ],
        states={
            T_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, templates_title)],
            T_BODY: [MessageHandler(filters.TEXT & ~filters.COMMAND, templates_body)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        per_message=False,
        conversation_timeout=300,
        name="templates_conv",
        persistent=False,
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    # Кнопки меню — до диалогов, чтобы перехватывать нажатия даже внутри разговора
    app.add_handler(MessageHandler(filters.Text(MENU_BUTTON_TEXTS), main_menu_button))
    app.add_handler(CallbackQueryHandler(handle_token_delete, pattern=r"^settoken_delete$"))
    app.add_handler(token_conv)
    app.add_handler(groups_conv)
    app.add_handler(templates_conv)
    app.add_handler(upload_conv)
    app.add_handler(CallbackQueryHandler(handle_cancel_upload, pattern="^cancel_upload$"))

    logger.info("Бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
