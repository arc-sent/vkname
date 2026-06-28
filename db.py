"""SQLite слой хранилища: пользователи, их VK-группы, заготовки описаний и логи ошибок."""

import os
import re
import sqlite3
import time

DATA_DIR = os.getenv("DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "bot.db")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                vk_token    TEXT
            );

            CREATE TABLE IF NOT EXISTS vk_groups (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                vk_group_id INTEGER NOT NULL,
                name        TEXT NOT NULL,
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id) ON DELETE CASCADE,
                UNIQUE (telegram_id, vk_group_id)
            );

            CREATE TABLE IF NOT EXISTS description_templates (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                title       TEXT NOT NULL,
                body        TEXT NOT NULL,
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS error_logs (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id   INTEGER NOT NULL,
                created_at    INTEGER NOT NULL,        -- unix-время (UTC, секунды)
                stage         TEXT,                    -- этап, на котором упало
                platform      TEXT,                    -- tiktok / likee / vk
                url           TEXT,                    -- исходная ссылка
                vk_group_id   INTEGER,
                vk_group_name TEXT,
                error_code    INTEGER,                 -- код ошибки VK (если есть)
                message       TEXT,                    -- короткое сообщение
                traceback     TEXT                     -- полная «транскрипция»
            );

            CREATE INDEX IF NOT EXISTS idx_error_logs_user
                ON error_logs (telegram_id, created_at DESC);
            """
        )


# ─── Пользователи ─────────────────────────────────────────────────────────────

def ensure_user(telegram_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (telegram_id) VALUES (?)",
            (telegram_id,),
        )


def get_vk_token(telegram_id: int) -> str | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT vk_token FROM users WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
    return row["vk_token"] if row else None


def set_vk_token(telegram_id: int, token: str) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO users (telegram_id, vk_token) VALUES (?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET vk_token = excluded.vk_token
            """,
            (telegram_id, token),
        )


def clear_vk_token(telegram_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE users SET vk_token = NULL WHERE telegram_id = ?",
            (telegram_id,),
        )


# ─── Группы VK ────────────────────────────────────────────────────────────────

def get_groups(telegram_id: int) -> list[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM vk_groups WHERE telegram_id = ? ORDER BY id",
            (telegram_id,),
        ).fetchall()


def get_group(group_row_id: int) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM vk_groups WHERE id = ?",
            (group_row_id,),
        ).fetchone()


def add_group(telegram_id: int, vk_group_id: int, name: str) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO vk_groups (telegram_id, vk_group_id, name) VALUES (?, ?, ?)
            ON CONFLICT(telegram_id, vk_group_id) DO UPDATE SET name = excluded.name
            """,
            (telegram_id, vk_group_id, name),
        )


def rename_group(group_row_id: int, name: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE vk_groups SET name = ? WHERE id = ?",
            (name, group_row_id),
        )


def delete_group(group_row_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM vk_groups WHERE id = ?", (group_row_id,))


# ─── Заготовки описаний ───────────────────────────────────────────────────────

def get_templates(telegram_id: int) -> list[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM description_templates WHERE telegram_id = ? ORDER BY id",
            (telegram_id,),
        ).fetchall()


def get_template(template_id: int) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM description_templates WHERE id = ?",
            (template_id,),
        ).fetchone()


def add_template(telegram_id: int, title: str, body: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO description_templates (telegram_id, title, body) VALUES (?, ?, ?)",
            (telegram_id, title, body),
        )


def update_template(template_id: int, title: str, body: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE description_templates SET title = ?, body = ? WHERE id = ?",
            (title, body, template_id),
        )


def delete_template(template_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM description_templates WHERE id = ?", (template_id,))


# ─── Логи ошибок ──────────────────────────────────────────────────────────────

# Маскировка VK-токенов перед записью в БД: текст исключения / traceback может
# содержать URL с access_token (GET-запросы к VK API) или сам токен vk1.a.*.
# Без этого чужие токены утекли бы в логи и в админ-панель.
_TOKEN_PATTERNS = [
    (re.compile(r"access_token=[^&\s\"'}]+"), "access_token=***"),
    (re.compile(r"vk1\.a\.[A-Za-z0-9._\-]+"), "vk1.a.***"),
]


def _sanitize(text: str | None) -> str | None:
    if not text:
        return text
    for pattern, repl in _TOKEN_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def log_error(
    telegram_id: int,
    *,
    stage: str | None = None,
    platform: str | None = None,
    url: str | None = None,
    vk_group_id: int | None = None,
    vk_group_name: str | None = None,
    error_code: int | None = None,
    message: str | None = None,
    traceback: str | None = None,
) -> None:
    """Записывает ошибку в БД. Best-effort: при сбое БД не роняет обработку."""
    try:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO error_logs
                    (telegram_id, created_at, stage, platform, url,
                     vk_group_id, vk_group_name, error_code, message, traceback)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    telegram_id,
                    int(time.time()),
                    stage,
                    platform,
                    _sanitize(url),
                    vk_group_id,
                    vk_group_name,
                    error_code,
                    _sanitize(message),
                    _sanitize(traceback),
                ),
            )
    except Exception:
        pass


def get_errors(telegram_id: int, limit: int = 8, offset: int = 0) -> list[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute(
            """
            SELECT * FROM error_logs WHERE telegram_id = ?
            ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?
            """,
            (telegram_id, limit, offset),
        ).fetchall()


def count_errors(telegram_id: int) -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM error_logs WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
    return row["c"] if row else 0


def get_error(error_id: int) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM error_logs WHERE id = ?",
            (error_id,),
        ).fetchone()


def get_users_with_errors(limit: int = 20, offset: int = 0) -> list[sqlite3.Row]:
    """Список пользователей с ошибками (для админ-панели): id, кол-во, последняя."""
    with _connect() as conn:
        return conn.execute(
            """
            SELECT telegram_id,
                   COUNT(*)        AS cnt,
                   MAX(created_at) AS last_at
            FROM error_logs
            GROUP BY telegram_id
            ORDER BY last_at DESC LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()


def count_users_with_errors() -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(DISTINCT telegram_id) AS c FROM error_logs"
        ).fetchone()
    return row["c"] if row else 0


def cleanup_old_errors(days: int) -> int:
    """Удаляет ошибки старше `days` суток. Возвращает число удалённых строк."""
    cutoff = int(time.time()) - days * 86400
    with _connect() as conn:
        cur = conn.execute("DELETE FROM error_logs WHERE created_at < ?", (cutoff,))
        return cur.rowcount
