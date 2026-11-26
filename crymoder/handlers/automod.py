"""Автомодерация: антифлуд, ссылки, инвайты, запрещённые слова. Регистрируется последней."""
from __future__ import annotations

import re
import time
from collections import defaultdict, deque

from aiogram import Bot, F, Router
from aiogram.enums import ChatType, MessageEntityType
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message

from .. import actions
from ..config import Config
from ..db import Database
from ..utils import Target, fmt_duration, is_anonymous_admin, is_staff, notify, user_mention

router = Router(name="automod")
router.message.filter(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))

INVITE_RE = re.compile(r"(?:t\.me|telegram\.me|telegram\.dog)/(?:\+|joinchat/|[A-Za-z0-9_]{4,})", re.IGNORECASE)
FLOOD_MUTE_SECONDS = 300
NOTICE_TTL = 10

_flood: dict[tuple[int, int], deque[float]] = defaultdict(deque)


def is_flooding(chat_id: int, user_id: int, limit: int, window: int, now: float | None = None) -> bool:
    now = time.monotonic() if now is None else now
    q = _flood[(chat_id, user_id)]
    q.append(now)
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= limit:
        q.clear()
        return True
    return False


def has_link(message: Message) -> bool:
    entities = (message.entities or []) + (message.caption_entities or [])
    return any(e.type in (MessageEntityType.URL, MessageEntityType.TEXT_LINK) for e in entities)


def has_invite(message: Message) -> bool:
    text = message.text or message.caption or ""
    if INVITE_RE.search(text):
        return True
    entities = (message.entities or []) + (message.caption_entities or [])
    return any(e.type == MessageEntityType.TEXT_LINK and e.url and INVITE_RE.search(e.url) for e in entities)


def find_bad_word(text: str, words: list[str]) -> str | None:
    norm = text.lower().replace("ё", "е")
    for w in words:
        if re.search(rf"(?<!\w){re.escape(w)}(?!\w)", norm):
            return w
    return None


@router.message()
async def automod(message: Message, bot: Bot, db: Database, config: Config) -> None:
    chat = message.chat
    settings = await db.get_settings(chat.id)

    # Служебные сообщения «вошёл/вышел»
    if message.new_chat_members or message.left_chat_member:
        if settings["delete_service"]:
            try:
                await message.delete()
            except TelegramAPIError:
                pass
        return

    user = message.from_user
    if (
        not user
        or user.is_bot
        or is_anonymous_admin(message)
        or message.is_automatic_forward          # пересылки из привязанного канала
        or message.sender_chat                   # сообщения от имени каналов
        or await is_staff(bot, chat.id, user.id)
    ):
        return

    target = Target(user.id, user.full_name)
    text = message.text or message.caption or ""

    async def punish_message(reason: str) -> None:
        try:
            await message.delete()
        except TelegramAPIError:
            return  # нет прав удалять — не наказываем вслепую
        verdict = await actions.apply_warn(
            bot, db, config, chat.id, chat.title or "", target, reason, bot.id
        )
        await notify(bot, chat.id, verdict, ttl=NOTICE_TTL)

    # 1. Антифлуд
    if settings["antiflood"] and is_flooding(chat.id, user.id, settings["flood_limit"], settings["flood_window"]):
        try:
            await actions.do_mute(bot, chat.id, user.id, FLOOD_MUTE_SECONDS)
            await actions.modlog(bot, db, config, chat.id, chat.title or "", bot.id, user.id, "auto-mute", "флуд")
            await notify(
                bot, chat.id,
                f"🔇 {user_mention(user)} в муте на {fmt_duration(FLOOD_MUTE_SECONDS)}: флуд.",
                ttl=NOTICE_TTL,
            )
        except TelegramAPIError:
            pass
        return

    # 2. Инвайт-ссылки на другие чаты / каналы
    if settings["antiinvite"] and has_invite(message):
        return await punish_message("реклама / инвайт-ссылка")

    # 3. Любые ссылки
    if settings["antilink"] and has_link(message):
        return await punish_message("ссылки запрещены")

    # 4. Запрещённые слова
    if settings["badwords"] and text:
        words = await db.get_words(chat.id)
        if words and find_bad_word(text, words):
            return await punish_message("запрещённое слово")
