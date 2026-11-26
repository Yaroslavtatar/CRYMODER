from __future__ import annotations

import time
from pathlib import Path

import aiosqlite

DEFAULTS: dict[str, int | str] = {
    "antiflood": 1,
    "flood_limit": 6,       # сообщений ...
    "flood_window": 8,      # ... за столько секунд
    "antilink": 0,          # любые ссылки
    "antiinvite": 1,        # инвайт-ссылки t.me/...
    "badwords": 1,
    "captcha": 1,
    "captcha_timeout": 120,
    "max_warns": 3,
    "warn_action": "mute",  # mute | ban | kick
    "delete_service": 0,    # удалять "X вступил / вышел"
}
RANGES = {
    "flood_limit": (3, 30),
    "flood_window": (3, 60),
    "max_warns": (1, 10),
    "captcha_timeout": (30, 600),
}
WARN_ACTIONS = ("mute", "ban", "kick")

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    chat_id INTEGER NOT NULL,
    key     TEXT    NOT NULL,
    value   TEXT    NOT NULL,
    PRIMARY KEY (chat_id, key)
);
CREATE TABLE IF NOT EXISTS warns (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    reason     TEXT    NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_warns ON warns (chat_id, user_id);
CREATE TABLE IF NOT EXISTS badwords (
    chat_id INTEGER NOT NULL,
    word    TEXT    NOT NULL,
    PRIMARY KEY (chat_id, word)
);
CREATE TABLE IF NOT EXISTS captcha (
    chat_id    INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    PRIMARY KEY (chat_id, user_id)
);
CREATE TABLE IF NOT EXISTS modlog (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id   INTEGER NOT NULL,
    admin_id  INTEGER NOT NULL,
    target_id INTEGER NOT NULL,
    action    TEXT    NOT NULL,
    reason    TEXT    NOT NULL DEFAULT '',
    ts        INTEGER NOT NULL
);
"""


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None
        self._settings: dict[int, dict] = {}
        self._words: dict[int, list[str]] = {}

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "Database.connect() не вызван"
        return self._conn

    async def connect(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()

    # ---------- settings ----------
    async def get_settings(self, chat_id: int) -> dict:
        cached = self._settings.get(chat_id)
        if cached is not None:
            return cached
        cur = await self.conn.execute("SELECT key, value FROM settings WHERE chat_id = ?", (chat_id,))
        settings = dict(DEFAULTS)
        for row in await cur.fetchall():
            key = row["key"]
            if key in DEFAULTS:
                settings[key] = int(row["value"]) if isinstance(DEFAULTS[key], int) else row["value"]
        self._settings[chat_id] = settings
        return settings

    async def set_setting(self, chat_id: int, key: str, value: int | str) -> None:
        if key not in DEFAULTS:
            raise KeyError(key)
        if key in RANGES:
            lo, hi = RANGES[key]
            value = max(lo, min(hi, int(value)))
        elif key == "warn_action":
            if value not in WARN_ACTIONS:
                raise ValueError(value)
        else:
            value = 1 if int(value) else 0
        await self.conn.execute(
            "INSERT INTO settings (chat_id, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT (chat_id, key) DO UPDATE SET value = excluded.value",
            (chat_id, key, str(value)),
        )
        await self.conn.commit()
        self._settings.pop(chat_id, None)

    # ---------- warns ----------
    async def add_warn(self, chat_id: int, user_id: int, reason: str = "") -> int:
        await self.conn.execute(
            "INSERT INTO warns (chat_id, user_id, reason, created_at) VALUES (?, ?, ?, ?)",
            (chat_id, user_id, reason, int(time.time())),
        )
        await self.conn.commit()
        return await self.warn_count(chat_id, user_id)

    async def warn_count(self, chat_id: int, user_id: int) -> int:
        cur = await self.conn.execute(
            "SELECT COUNT(*) AS c FROM warns WHERE chat_id = ? AND user_id = ?", (chat_id, user_id)
        )
        return (await cur.fetchone())["c"]

    async def list_warns(self, chat_id: int, user_id: int) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            "SELECT reason, created_at FROM warns WHERE chat_id = ? AND user_id = ? ORDER BY id",
            (chat_id, user_id),
        )
        return list(await cur.fetchall())

    async def remove_last_warn(self, chat_id: int, user_id: int) -> int:
        await self.conn.execute(
            "DELETE FROM warns WHERE id = (SELECT MAX(id) FROM warns WHERE chat_id = ? AND user_id = ?)",
            (chat_id, user_id),
        )
        await self.conn.commit()
        return await self.warn_count(chat_id, user_id)

    async def reset_warns(self, chat_id: int, user_id: int) -> None:
        await self.conn.execute("DELETE FROM warns WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
        await self.conn.commit()

    # ---------- bad words ----------
    async def get_words(self, chat_id: int) -> list[str]:
        cached = self._words.get(chat_id)
        if cached is not None:
            return cached
        cur = await self.conn.execute("SELECT word FROM badwords WHERE chat_id = ? ORDER BY word", (chat_id,))
        words = [r["word"] for r in await cur.fetchall()]
        self._words[chat_id] = words
        return words

    async def add_word(self, chat_id: int, word: str) -> None:
        await self.conn.execute("INSERT OR IGNORE INTO badwords (chat_id, word) VALUES (?, ?)", (chat_id, word))
        await self.conn.commit()
        self._words.pop(chat_id, None)

    async def del_word(self, chat_id: int, word: str) -> bool:
        cur = await self.conn.execute("DELETE FROM badwords WHERE chat_id = ? AND word = ?", (chat_id, word))
        await self.conn.commit()
        self._words.pop(chat_id, None)
        return cur.rowcount > 0

    # ---------- captcha ----------
    async def add_captcha(self, chat_id: int, user_id: int, message_id: int, expires_at: int) -> None:
        await self.conn.execute(
            "INSERT OR REPLACE INTO captcha (chat_id, user_id, message_id, expires_at) VALUES (?, ?, ?, ?)",
            (chat_id, user_id, message_id, expires_at),
        )
        await self.conn.commit()

    async def get_captcha(self, chat_id: int, user_id: int) -> aiosqlite.Row | None:
        cur = await self.conn.execute(
            "SELECT * FROM captcha WHERE chat_id = ? AND user_id = ?", (chat_id, user_id)
        )
        return await cur.fetchone()

    async def del_captcha(self, chat_id: int, user_id: int) -> None:
        await self.conn.execute("DELETE FROM captcha WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
        await self.conn.commit()

    async def all_captchas(self) -> list[aiosqlite.Row]:
        cur = await self.conn.execute("SELECT * FROM captcha")
        return list(await cur.fetchall())

    # ---------- modlog ----------
    async def log(self, chat_id: int, admin_id: int, target_id: int, action: str, reason: str = "") -> None:
        await self.conn.execute(
            "INSERT INTO modlog (chat_id, admin_id, target_id, action, reason, ts) VALUES (?, ?, ?, ?, ?, ?)",
            (chat_id, admin_id, target_id, action, reason, int(time.time())),
        )
        await self.conn.commit()
