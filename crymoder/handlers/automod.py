"""Автомодерация: антифлуд, ссылки, инвайты, запрещённые слова. Вызывается из общего конвейера сообщений
(events.on_group_message) после защиты чата и до разбора команд. Наказания идут через Core (варны, муты),
поэтому лимит предупреждений, ранги и журнал у автомодерации и ручных команд общие."""
from __future__ import annotations

import re
import time
from collections import defaultdict, deque

from aiogram import Bot
from aiogram.enums import MessageEntityType
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message

from ..core import Core
from ..utils import is_anonymous_admin, is_staff, notify

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


async def check(core: Core, bot: Bot, message: Message) -> bool:
    """Применяет автомодерацию. True — сообщение признано нарушением (дальше его разбирать не нужно)."""
    chat = message.chat
    user = message.from_user
    if (
        not user
        or user.is_bot
        or is_anonymous_admin(message)
        or message.is_automatic_forward          # пересылки из привязанного канала
        or message.sender_chat                   # сообщения от имени каналов
        or core.rank_of(chat.id, user.id) >= 1   # модераторы бота
        or await is_staff(bot, chat.id, user.id)  # любые админы Telegram
    ):
        return False

    get = core.get
    text = message.text or message.caption or ""

    async def punish_message(reason: str) -> bool:
        try:
            await message.delete()
        except TelegramAPIError:
            return False  # нет прав удалять — не наказываем вслепую
        r = await core.warn(chat.id, user.id, 0, None, reason, "auto")
        await notify(bot, chat.id, r.text, ttl=NOTICE_TTL)
        return True

    # 1. Антифлуд
    if get(chat.id, "antiflood") and is_flooding(chat.id, user.id, get(chat.id, "flood_limit"),
                                                 get(chat.id, "flood_window")):
        r = await core.mute(chat.id, user.id, 0, FLOOD_MUTE_SECONDS, "флуд", "auto")
        if r.ok:
            await notify(bot, chat.id, r.text, ttl=NOTICE_TTL)
        return True

    # 2. Инвайт-ссылки на другие чаты / каналы
    if get(chat.id, "antiinvite") and has_invite(message):
        return await punish_message("реклама / инвайт-ссылка")

    # 3. Любые ссылки
    if get(chat.id, "antilink") and has_link(message):
        return await punish_message("ссылки запрещены")

    # 4. Запрещённые слова
    if get(chat.id, "badwords") and text:
        words = core.words(chat.id)
        if words and find_bad_word(text, words):
            return await punish_message("запрещённое слово")
    return False
