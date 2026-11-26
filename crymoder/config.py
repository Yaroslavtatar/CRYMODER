from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: str = ".env") -> None:
    """Минимальный парсер .env (без внешних зависимостей)."""
    p = Path(path)
    if not p.is_file():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


@dataclass(frozen=True)
class Config:
    token: str
    owner_ids: frozenset[int]
    db_path: str
    log_chat_id: int | None
    log_level: str


def load_config() -> Config:
    _load_dotenv()
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token or token == "123456:replace_me":
        raise SystemExit("BOT_TOKEN не задан. Скопируйте .env.example в .env и впишите токен от @BotFather.")

    owners = frozenset(
        int(x) for x in os.getenv("OWNER_IDS", "").replace(" ", "").split(",") if x.lstrip("-").isdigit()
    )
    log_chat = os.getenv("LOG_CHAT_ID", "").strip()
    return Config(
        token=token,
        owner_ids=owners,
        db_path=os.getenv("DB_PATH", "data/crymoder.db"),
        log_chat_id=int(log_chat) if log_chat.lstrip("-").isdigit() else None,
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
    )
