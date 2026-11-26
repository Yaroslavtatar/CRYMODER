from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from html import escape

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.types import Message, User

_DUR_RE = re.compile(r"^(\d+)([smhdw])$", re.IGNORECASE)
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_MIN_SECONDS = 30                # Telegram считает <30 c "навсегда"
_MAX_SECONDS = 366 * 86400

_bg_tasks: set[asyncio.Task] = set()


def spawn(coro) -> asyncio.Task:
    """create_task с удержанием ссылки, чтобы задачу не собрал GC."""
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return task


def parse_duration(text: str) -> int | None:
    """'10m' -> 600. None, если строка не похожа на длительность."""
    m = _DUR_RE.match(text.strip())
    if not m:
        return None
    seconds = int(m.group(1)) * _UNITS[m.group(2).lower()]
    return max(_MIN_SECONDS, min(_MAX_SECONDS, seconds))


def fmt_duration(seconds: int | None) -> str:
    if not seconds:
        return "навсегда"
    for unit, name in ((86400, "д"), (3600, "ч"), (60, "мин")):
        if seconds >= unit:
            return f"{seconds // unit} {name}"
    return f"{seconds} с"


def mention(user_id: int, name: str) -> str:
    return f'<a href="tg://user?id={user_id}">{escape(name or str(user_id))}</a>'


def user_mention(user: User) -> str:
    return mention(user.id, user.full_name)


# ---------- права ----------
_admin_cache: dict[tuple[int, int], tuple[float, bool]] = {}
_CACHE_TTL = 60


async def can_restrict(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Создатель или админ с правом блокировки участников (кэш на 60 с)."""
    key = (chat_id, user_id)
    hit = _admin_cache.get(key)
    now = time.monotonic()
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1]
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except Exception:
        return False
    if member.status == ChatMemberStatus.CREATOR:
        ok = True
    elif member.status == ChatMemberStatus.ADMINISTRATOR:
        ok = bool(getattr(member, "can_restrict_members", False))
    else:
        ok = False
    _admin_cache[key] = (now, ok)
    return ok


async def is_staff(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Любой админ чата (нужен, чтобы автомодерация не трогала админов)."""
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except Exception:
        return False
    return member.status in (ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR)


def is_anonymous_admin(message: Message) -> bool:
    return bool(message.sender_chat and message.sender_chat.id == message.chat.id)


# ---------- цель команды ----------
@dataclass
class Target:
    id: int
    name: str

    @property
    def mention(self) -> str:
        return mention(self.id, self.name)


def resolve_target(message: Message, args: list[str]) -> tuple[Target | None, list[str]]:
    """Цель — автор сообщения, на которое ответили, либо числовой ID первым аргументом."""
    reply = message.reply_to_message
    if reply and reply.from_user and not reply.forum_topic_created:
        u = reply.from_user
        return Target(u.id, u.full_name), args
    if args and args[0].lstrip("-").isdigit():
        uid = int(args[0])
        return Target(uid, str(uid)), args[1:]
    return None, args


async def notify(bot: Bot, chat_id: int, text: str, ttl: int = 0) -> None:
    msg = await bot.send_message(chat_id, text, disable_notification=True)
    if ttl:
        spawn(_delete_later(msg, ttl))


async def _delete_later(msg: Message, ttl: int) -> None:
    await asyncio.sleep(ttl)
    try:
        await msg.delete()
    except Exception:
        pass
