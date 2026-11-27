"""Настройки из переменных окружения / файла .env."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_env_file() -> None:
    p = ROOT / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env_file()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
PUBLIC_URL = os.getenv("PUBLIC_URL", "http://localhost:8000").rstrip("/")
WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("WEB_PORT", "8000"))
DB_PATH = os.getenv("DB_PATH", str(ROOT / "data" / "chatmanager.sqlite3"))
# Часовой пояс для статистики по дням (часы от UTC). Iris считает по МСК.
TZ_OFFSET_HOURS = int(os.getenv("TZ_OFFSET_HOURS", "3"))
# Кому разрешено управлять ботом глобально (через запятую, необязательно).
SUPER_ADMINS = {int(x) for x in os.getenv("SUPER_ADMINS", "").split(",") if x.strip().isdigit()}
