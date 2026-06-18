"""SQLite слой хранилища: пользователи, их VK-группы и заготовки описаний."""

import sqlite3
import os

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
