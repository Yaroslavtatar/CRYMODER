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


def _ids(name: str) -> frozenset[int]:
    return frozenset(int(x) for x in os.getenv(name, "").replace(" ", "").split(",") if x.lstrip("-").isdigit())


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"{name} должен быть числом, а не «{raw}».")


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on", "да")


@dataclass(frozen=True)
class Config:
    token: str
    owner_ids: frozenset[int]
    db_path: str
    log_chat_id: int | None
    log_level: str
    # веб-панель и общие настройки (значения по умолчанию — чтобы Config(...) в тестах оставался коротким)
    public_url: str = "http://localhost:8000"
    web_enabled: bool = True
    web_host: str = "0.0.0.0"
    web_port: int = 8000
    tz_offset_hours: int = 3
    super_admins: frozenset[int] = frozenset()   # владельцы бота: полный доступ ко всем чатам и панели


def load_config() -> Config:
    _load_dotenv()
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token or token == "123456:replace_me":
        raise SystemExit("BOT_TOKEN не задан. Скопируйте .env.example в .env и впишите токен от @BotFather.")

    owners = _ids("OWNER_IDS")
    log_chat = os.getenv("LOG_CHAT_ID", "").strip()
    return Config(
        token=token,
        owner_ids=owners,
        db_path=os.getenv("DB_PATH", "data/crymoder.db"),
        log_chat_id=int(log_chat) if log_chat.lstrip("-").isdigit() else None,
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        public_url=os.getenv("PUBLIC_URL", "http://localhost:8000").strip().rstrip("/"),
        web_enabled=_bool("WEB_ENABLED", True),
        web_host=os.getenv("WEB_HOST", "0.0.0.0").strip(),
        web_port=_int("WEB_PORT", 8000),
        tz_offset_hours=_int("TZ_OFFSET_HOURS", 3),
        super_admins=owners | _ids("SUPER_ADMINS"),
    )
