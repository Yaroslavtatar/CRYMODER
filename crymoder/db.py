"""Единая база бота: тонкая обёртка над sqlite3, схема и миграция со старых версий."""
import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats(
  chat_id INTEGER PRIMARY KEY, title TEXT, code TEXT UNIQUE, creator_id INTEGER,
  grid_id INTEGER, created_at INTEGER, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS settings(
  chat_id INTEGER, key TEXT, value TEXT, PRIMARY KEY(chat_id, key));
CREATE TABLE IF NOT EXISTS users(
  user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, last_name TEXT,
  is_bot INTEGER DEFAULT 0, first_seen INTEGER);
CREATE INDEX IF NOT EXISTS ix_users_username ON users(username);
CREATE TABLE IF NOT EXISTS members(
  chat_id INTEGER, user_id INTEGER, first_seen INTEGER, last_seen INTEGER,
  msg_count INTEGER DEFAULT 0, joined_at INTEGER, left INTEGER DEFAULT 0,
  invited_by INTEGER, PRIMARY KEY(chat_id, user_id));
CREATE TABLE IF NOT EXISTS msg_daily(
  chat_id INTEGER, user_id INTEGER, day TEXT, cnt INTEGER DEFAULT 0,
  PRIMARY KEY(chat_id, user_id, day));
CREATE TABLE IF NOT EXISTS msgs(
  chat_id INTEGER, message_id INTEGER, user_id INTEGER, ts INTEGER,
  PRIMARY KEY(chat_id, message_id));
CREATE TABLE IF NOT EXISTS ranks(
  chat_id INTEGER, user_id INTEGER, rank INTEGER, appointed_by INTEGER, at INTEGER,
  PRIMARY KEY(chat_id, user_id));
CREATE TABLE IF NOT EXISTS rank_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, user_id INTEGER,
  old INTEGER, new INTEGER, by_id INTEGER, at INTEGER);
CREATE TABLE IF NOT EXISTS warns(
  id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, user_id INTEGER,
  by_id INTEGER, reason TEXT, created_at INTEGER, expires_at INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_warns ON warns(chat_id, user_id);
CREATE TABLE IF NOT EXISTS mutes(
  chat_id INTEGER, user_id INTEGER, by_id INTEGER, reason TEXT, until INTEGER, created_at INTEGER,
  PRIMARY KEY(chat_id, user_id));
CREATE TABLE IF NOT EXISTS bans(
  chat_id INTEGER, user_id INTEGER, by_id INTEGER, reason TEXT, until INTEGER, created_at INTEGER,
  PRIMARY KEY(chat_id, user_id));
CREATE TABLE IF NOT EXISTS cmd_access(
  chat_id INTEGER, code TEXT, rank INTEGER, PRIMARY KEY(chat_id, code));
CREATE TABLE IF NOT EXISTS cmd_access_user(
  chat_id INTEGER, user_id INTEGER, code TEXT, allowed INTEGER,
  PRIMARY KEY(chat_id, user_id, code));
CREATE TABLE IF NOT EXISTS access_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, code TEXT, old INTEGER, new INTEGER,
  by_id INTEGER, at INTEGER);
CREATE TABLE IF NOT EXISTS profiles(
  chat_id INTEGER, user_id INTEGER, nick TEXT, title TEXT, motto TEXT, about TEXT, gender TEXT,
  PRIMARY KEY(chat_id, user_id));
CREATE TABLE IF NOT EXISTS rep(
  chat_id INTEGER, user_id INTEGER, plus INTEGER DEFAULT 0, star INTEGER DEFAULT 0,
  PRIMARY KEY(chat_id, user_id));
CREATE TABLE IF NOT EXISTS votes(
  id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, target_id INTEGER, need INTEGER,
  min_rank INTEGER, message_id INTEGER, by_id INTEGER, created_at INTEGER, closed INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS vote_ballots(
  vote_id INTEGER, user_id INTEGER, PRIMARY KEY(vote_id, user_id));
CREATE TABLE IF NOT EXISTS allowed_links(
  chat_id INTEGER, link TEXT, PRIMARY KEY(chat_id, link));
CREATE TABLE IF NOT EXISTS leaves(
  chat_id INTEGER, user_id INTEGER, at INTEGER);
CREATE TABLE IF NOT EXISTS grids(
  grid_id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER, code TEXT UNIQUE, created_at INTEGER);
CREATE TABLE IF NOT EXISTS grid_roles(
  grid_id INTEGER, user_id INTEGER, role TEXT, PRIMARY KEY(grid_id, user_id));
CREATE TABLE IF NOT EXISTS gbans(
  grid_id INTEGER, user_id INTEGER, by_id INTEGER, reason TEXT, at INTEGER,
  PRIMARY KEY(grid_id, user_id));
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, by_id INTEGER, action TEXT,
  detail TEXT, source TEXT, at INTEGER);
CREATE INDEX IF NOT EXISTS ix_audit ON audit(chat_id, id);
CREATE TABLE IF NOT EXISTS web_tokens(
  token TEXT PRIMARY KEY, user_id INTEGER, expires_at INTEGER);
CREATE TABLE IF NOT EXISTS web_sessions(
  sid TEXT PRIMARY KEY, user_id INTEGER, expires_at INTEGER);
CREATE TABLE IF NOT EXISTS badwords(
  chat_id INTEGER, word TEXT, PRIMARY KEY(chat_id, word));
CREATE TABLE IF NOT EXISTS captcha(
  chat_id INTEGER, user_id INTEGER, message_id INTEGER, expires_at INTEGER,
  PRIMARY KEY(chat_id, user_id));
"""


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def migrate(conn: sqlite3.Connection) -> None:
    """Приводит базы старых версий CRYMODER к текущей схеме (идемпотентно)."""
    cols = _cols(conn, "warns")
    if "by_id" not in cols:
        conn.execute("ALTER TABLE warns ADD COLUMN by_id INTEGER DEFAULT 0")
    if "expires_at" not in cols:
        conn.execute("ALTER TABLE warns ADD COLUMN expires_at INTEGER DEFAULT 0")
    # раньше лимит предупреждений хранился как max_warns, теперь — warn_limit
    conn.execute("INSERT OR IGNORE INTO settings(chat_id,key,value) "
                 "SELECT chat_id,'warn_limit',value FROM settings WHERE key='max_warns'")


class DB:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.executescript(SCHEMA)
            migrate(self.conn)
            self.conn.commit()

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    def execute(self, sql: str, args=()) -> int:
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur.lastrowid

    def query(self, sql: str, args=()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    def one(self, sql: str, args=()):
        with self.lock:
            return self.conn.execute(sql, args).fetchone()

    def scalar(self, sql: str, args=(), default=None):
        r = self.one(sql, args)
        return r[0] if r and r[0] is not None else default
