"""Скачивание видео с TikTok, Likee и VK."""

import asyncio
import logging
import os
import re
import ssl
import tempfile
import uuid

import urllib3
import yt_dlp
import requests
from requests import Session
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context

urllib3.disable_warnings()
logger = logging.getLogger(__name__)

MAX_VIDEO_DURATION = 180  # секунд (3 минуты)


# ─── HTTP сессия с ослабленным SSL ───────────────────────────────────────────

class _PermissiveSSLAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        ctx = create_urllib3_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.set_ciphers("DEFAULT@SECLEVEL=0")
        if hasattr(ssl, "OP_LEGACY_SERVER_CONNECT"):
            ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT
        kwargs["ssl_context"] = ctx
        super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, proxy, **kwargs):
        ctx = create_urllib3_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.set_ciphers("DEFAULT@SECLEVEL=0")
        if hasattr(ssl, "OP_LEGACY_SERVER_CONNECT"):
            ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT
        kwargs["ssl_context"] = ctx
        return super().proxy_manager_for(proxy, **kwargs)


def _make_session() -> Session:
    s = Session()
    adapter = _PermissiveSSLAdapter()
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def _tmp_path(prefix: str) -> str:
    d = os.path.join(tempfile.gettempdir(), "vk_parser_bot")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{prefix}_{uuid.uuid4().hex}")


# ─── Определение платформы ────────────────────────────────────────────────────

_TIKTOK_RE = re.compile(r"tiktok\.com|vm\.tiktok\.com|vt\.tiktok\.com", re.IGNORECASE)
_LIKEE_RE  = re.compile(r"https?://(?:l\.)?likee\.video/", re.IGNORECASE)
_VK_RE     = re.compile(r"https?://(?:(?:www|m)\.)?vk\.(?:com|ru)/(?:video|clips?)", re.IGNORECASE)


def detect_platform(url: str) -> str | None:
    """Возвращает 'tiktok', 'likee', 'vk' или None."""
    if _TIKTOK_RE.search(url):
        return "tiktok"
    if _LIKEE_RE.match(url):
        return "likee"
    if _VK_RE.match(url):
        return "vk"
    return None


# ─── TikTok (yt-dlp) ─────────────────────────────────────────────────────────

def _download_tiktok_sync(url: str, save_path: str | None) -> tuple[str, str]:
    # Фаза 1: получаем метаданные без скачивания — проверяем длительность
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
        meta = ydl.extract_info(url, download=False)
    _check_duration(meta.get("duration"))

    # Фаза 2: скачиваем
    tmpdir = save_path or _tmp_path("tiktok_dir")
    if save_path:
        os.makedirs(save_path, exist_ok=True)
    else:
        os.makedirs(tmpdir, exist_ok=True)

    ydl_opts = {
        "outtmpl": os.path.join(tmpdir, "%(id)s.%(ext)s"),
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)

    title = (info.get("title") or "TikTok Video")[:100]
    files = [f for f in os.listdir(tmpdir) if os.path.isfile(os.path.join(tmpdir, f))]
    if not files:
        raise RuntimeError("Файл TikTok не был скачан")
    return os.path.join(tmpdir, files[0]), title


async def download_tiktok(url: str, save_path: str | None = None) -> tuple[str, str]:
    """Возвращает (путь к файлу, название)."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _download_tiktok_sync, url, save_path)


# ─── Likee ────────────────────────────────────────────────────────────────────

_LIKEE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/20A362"
    ),
    "Referer": "https://likee.video/",
}


def _meta(html: str, prop: str) -> str:
    m = re.search(
        r'<meta[^>]+(?:property|name)=["\']' + re.escape(prop) + r'["\'][^>]+content=["\']([^"\']+)["\']',
        html, re.IGNORECASE,
    ) or re.search(
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']' + re.escape(prop) + r'["\']',
        html, re.IGNORECASE,
    )
    return m.group(1) if m else ""


def _get_likee_info_sync(url: str) -> dict:
    resp = _make_session().get(url, headers=_LIKEE_HEADERS, allow_redirects=True, proxies=None, timeout=15)
    html = resp.text

    video_url = _meta(html, "og:video:secure_url") or _meta(html, "og:video")
    if not video_url:
        raise ValueError("Не удалось найти видео на странице Likee. Видео может быть удалено или недоступно.")

    post_id = ""
    m = re.search(r'postid=(\d+)', html, re.IGNORECASE) or re.search(r'/video/(\d+)', resp.url)
    if m:
        post_id = m.group(1)

    title = _meta(html, "og:title") or post_id or "Likee Video"
    duration_str = _meta(html, "video:duration") or _meta(html, "og:video:duration")
    duration = int(duration_str) if duration_str and duration_str.isdigit() else None
    return {"video_url": video_url, "title": title, "duration": duration}


def _download_likee_sync(url: str) -> tuple[str, str]:
    info = _get_likee_info_sync(url)
    _check_duration(info.get("duration"))
    out_path = _tmp_path("likee") + ".mp4"

    with _make_session().get(
        info["video_url"], headers=_LIKEE_HEADERS, stream=True, proxies=None, timeout=120,
    ) as r:
        r.raise_for_status()
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                f.write(chunk)

    return out_path, info["title"]


async def download_likee(url: str) -> tuple[str, str]:
    """Возвращает (путь к файлу, название)."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _download_likee_sync, url)


# ─── VK ───────────────────────────────────────────────────────────────────────

_DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 12; Pixel 6) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36"
)
_QUALITY_ORDER = [2160, 1440, 1080, 720, 480, 360, 240]


def _find_mp4(text: str) -> tuple[str, int] | None:
    for height in _QUALITY_ORDER:
        for key in (f"mp4_{height}", f"url{height}"):
            m = re.search(r'"' + re.escape(key) + r'"\s*:\s*"([^"]+)"', text)
            if m:
                url = m.group(1).replace("\\/", "/")
                if url.startswith("http"):
                    return url, height
    return None


def _find_duration(text: str) -> int | None:
    """Ищет поле duration (секунды) в JSON-подобном тексте ответа."""
    m = re.search(r'"duration"\s*:\s*(\d+)', text)
    return int(m.group(1)) if m else None


def _check_duration(duration: int | None) -> None:
    """Бросает ValueError если длительность превышает лимит."""
    if duration and duration > MAX_VIDEO_DURATION:
        mins, secs = divmod(duration, 60)
        raise ValueError(f"Видео слишком длинное — {mins}:{secs:02d}. Максимум 3 минуты.")


def _extract_vk_ids(url: str) -> tuple[str, str] | None:
    m = re.search(r'(?:clip|video)(-?\d+)_(\d+)', url)
    return (m.group(1), m.group(2)) if m else None


def _to_vkcom(url: str) -> str:
    url = re.sub(r'https?://(?:www\.)?vk\.ru/', 'https://vk.com/', url, flags=re.IGNORECASE)
    url = re.sub(r'https?://m\.vk\.com/', 'https://vk.com/', url, flags=re.IGNORECASE)
    return url


def _og_title(html: str) -> str:
    m = re.search(
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
        html, re.IGNORECASE,
    ) or re.search(
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']',
        html, re.IGNORECASE,
    )
    return m.group(1).strip() if m else ""


def _try_embed(session: Session, oid: str, vid: str) -> tuple[str, int, int | None] | None:
    try:
        resp = session.get(
            f"https://vk.com/video_ext.php?oid={oid}&id={vid}&hd=1",
            headers={"User-Agent": _DESKTOP_UA, "Accept-Language": "ru-RU,ru;q=0.9"},
            verify=False, timeout=12,
        )
        result = _find_mp4(resp.text)
        if result:
            return result[0], result[1], _find_duration(resp.text)
    except Exception as e:
        logger.debug("VK embed failed: %s", e)
    return None


def _try_ajax(session: Session, oid: str, vid: str) -> tuple[str, int, int | None] | None:
    try:
        session.get("https://vk.com/", headers={"User-Agent": _DESKTOP_UA}, verify=False, timeout=8)
    except Exception:
        pass
    try:
        resp = session.post(
            "https://vk.com/al_video.php",
            data={"act": "show", "al": "1", "video": f"{oid}_{vid}"},
            headers={
                "User-Agent": _DESKTOP_UA,
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "text/javascript, text/html, application/xml, text/xml, */*",
                "Origin": "https://vk.com",
                "Referer": f"https://vk.com/video{oid}_{vid}",
                "Accept-Language": "ru-RU,ru;q=0.9",
            },
            verify=False, timeout=12,
        )
        result = _find_mp4(resp.text)
        if result:
            return result[0], result[1], _find_duration(resp.text)
    except Exception as e:
        logger.debug("VK ajax failed: %s", e)
    return None


def _try_vk_api(oid: str, vid: str, token: str) -> tuple[str, int, int | None] | None:
    try:
        data = _make_session().get(
            "https://api.vk.com/method/video.get",
            params={"videos": f"{oid}_{vid}", "access_token": token, "v": "5.131"},
            timeout=15,
        ).json()
        items = data.get("response", {}).get("items", [])
        if not items:
            return None
        files = items[0].get("files", {})
        duration = items[0].get("duration")
        for height in _QUALITY_ORDER:
            if f"mp4_{height}" in files:
                return files[f"mp4_{height}"], height, duration
    except Exception as e:
        logger.debug("VK api failed: %s", e)
    return None


def _try_mobile(session: Session, url: str) -> tuple[tuple[str, int, int | None] | None, str]:
    mobile_url = re.sub(r'https://vk\.com/', 'https://m.vk.com/', url, flags=re.IGNORECASE)
    try:
        resp = session.get(
            mobile_url,
            headers={"User-Agent": _MOBILE_UA, "Accept-Language": "ru-RU,ru;q=0.9"},
            allow_redirects=True, verify=False, timeout=12,
        )
        result = _find_mp4(resp.text)
        if result:
            return (result[0], result[1], _find_duration(resp.text)), _og_title(resp.text)
    except Exception as e:
        logger.debug("VK mobile failed: %s", e)
    return None, ""


def _get_vk_info_sync(url: str, vk_token: str | None) -> dict:
    url = _to_vkcom(url)
    ids = _extract_vk_ids(url)
    session = _make_session()
    title = "VK видео"

    if ids:
        oid, vid = ids

        result = _try_embed(session, oid, vid)
        if result:
            video_url, quality, duration = result
            logger.info("VK embed: %dp", quality)
            return {"video_url": video_url, "title": title, "duration": duration}

        result = _try_ajax(session, oid, vid)
        if result:
            video_url, quality, duration = result
            logger.info("VK ajax: %dp", quality)
            return {"video_url": video_url, "title": title, "duration": duration}

        if vk_token:
            result = _try_vk_api(oid, vid, vk_token)
            if result:
                video_url, quality, duration = result
                logger.info("VK api: %dp", quality)
                return {"video_url": video_url, "title": title, "duration": duration}

    result, og_title = _try_mobile(session, url)
    if result:
        video_url, quality, duration = result
        logger.info("VK mobile: %dp", quality)
        return {"video_url": video_url, "title": og_title or title, "duration": duration}

    raise ValueError(
        "Не удалось извлечь ссылку на видео VK.\n"
        "Видео может быть приватным или требовать авторизации."
    )


def _download_vk_sync(url: str, vk_token: str | None) -> tuple[str, str]:
    info = _get_vk_info_sync(url, vk_token)
    _check_duration(info.get("duration"))
    out_path = _tmp_path("vk") + ".mp4"

    with _make_session().get(
        info["video_url"],
        headers={"User-Agent": _DESKTOP_UA, "Referer": "https://vk.com/"},
        stream=True, timeout=180,
    ) as r:
        r.raise_for_status()
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                f.write(chunk)

    return out_path, info["title"]


async def download_vk(url: str, vk_token: str | None = None) -> tuple[str, str]:
    """Возвращает (путь к файлу, название). vk_token улучшает шанс успеха."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _download_vk_sync, url, vk_token)
