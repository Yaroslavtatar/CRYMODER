"""Капча для новичков: вошёл → ограничен → нажал кнопку → свободен. Не нажал → кик.
Запуск капчи — из общего обработчика входов (events.on_join), здесь же — кнопка и восстановление таймеров."""
from __future__ import annotations

import asyncio
import logging
import time

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup

from ..core import Core
from ..utils import spawn, user_mention

log = logging.getLogger(__name__)
router = Router(name="captcha")

_tasks: dict[tuple[int, int], asyncio.Task] = {}

_SEND_FLAGS = ("can_send_messages", "can_send_audios", "can_send_documents", "can_send_photos", "can_send_videos",
               "can_send_video_notes", "can_send_voice_notes", "can_send_polls", "can_send_other_messages",
               "can_add_web_page_previews")
MUTE_PERMS = ChatPermissions(**{k: False for k in _SEND_FLAGS})
FULL_PERMS = ChatPermissions(**{k: True for k in _SEND_FLAGS})


async def _release(bot: Bot, chat_id: int, user_id: int) -> None:
    """Снимает ограничение капчи, возвращая стандартные права чата."""
    try:
        chat = await bot.get_chat(chat_id)
        perms = (chat.permissions or FULL_PERMS).model_copy(update={"can_send_messages": True})
    except TelegramAPIError:
        perms = FULL_PERMS
    await bot.restrict_chat_member(chat_id, user_id, permissions=perms)


async def _expire(bot: Bot, core: Core, chat_id: int, user_id: int, message_id: int, delay: float) -> None:
    try:
        await asyncio.sleep(max(0, delay))
    except asyncio.CancelledError:
        return
    if not core.captcha_get(chat_id, user_id):
        return  # уже прошёл капчу
    core.captcha_del(chat_id, user_id)
    _tasks.pop((chat_id, user_id), None)
    r = await core.kick(chat_id, user_id, 0, "не прошёл капчу", "auto")
    if not r.ok:
        log.warning("Капча: не удалось кикнуть %s в %s: %s", user_id, chat_id, r.text)
    try:
        await bot.delete_message(chat_id, message_id)
    except TelegramAPIError:
        pass


def _schedule(bot: Bot, core: Core, chat_id: int, user_id: int, message_id: int, delay: float) -> None:
    key = (chat_id, user_id)
    old = _tasks.pop(key, None)
    if old:
        old.cancel()
    _tasks[key] = spawn(_expire(bot, core, chat_id, user_id, message_id, delay))


async def start(core: Core, bot: Bot, chat_id: int, user) -> bool:
    """Ограничивает новичка и просит нажать кнопку. False — капча не запущена."""
    timeout = core.get(chat_id, "captcha_timeout")
    try:
        await bot.restrict_chat_member(chat_id, user.id, permissions=MUTE_PERMS)
    except TelegramAPIError as exc:
        log.warning("Капча: нет прав ограничить %s в %s: %s", user.id, chat_id, exc)
        return False
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🤖 Я не бот", callback_data=f"cap:{user.id}")]])
    msg = await bot.send_message(
        chat_id,
        f"👋 {user_mention(user)}, добро пожаловать!\n"
        f"Нажмите кнопку в течение {timeout} с, иначе вы будете исключены.",
        reply_markup=kb)
    core.captcha_add(chat_id, user.id, msg.message_id, int(time.time()) + timeout)
    _schedule(bot, core, chat_id, user.id, msg.message_id, timeout)
    return True


async def forget(core: Core, bot: Bot, chat_id: int, user_id: int) -> None:
    """Участник вышел, не пройдя капчу: убираем таймер и сообщение."""
    row = core.captcha_get(chat_id, user_id)
    if not row:
        return
    core.captcha_del(chat_id, user_id)
    task = _tasks.pop((chat_id, user_id), None)
    if task:
        task.cancel()
    try:
        await bot.delete_message(chat_id, row["message_id"])
    except TelegramAPIError:
        pass


@router.callback_query(F.data.startswith("cap:"))
async def on_captcha_press(call: CallbackQuery, bot: Bot, core: Core) -> None:
    if not call.message:
        return await call.answer()
    user_id = int(call.data.split(":")[1])
    if call.from_user.id != user_id:
        return await call.answer("Эта кнопка не для вас.", show_alert=True)
    chat_id = call.message.chat.id
    if not core.captcha_get(chat_id, user_id):
        return await call.answer()
    core.captcha_del(chat_id, user_id)
    task = _tasks.pop((chat_id, user_id), None)
    if task:
        task.cancel()
    try:
        await _release(bot, chat_id, user_id)
    except TelegramAPIError as exc:
        log.warning("Капча: не удалось снять ограничения с %s: %s", user_id, exc)
    await call.answer("Добро пожаловать! ✅")
    try:
        await call.message.delete()
    except TelegramAPIError:
        pass


async def restore(bot: Bot, core: Core) -> None:
    """После рестарта бота перезапускаем таймеры незавершённых капч."""
    now = time.time()
    for row in core.captcha_all():
        _schedule(bot, core, row["chat_id"], row["user_id"], row["message_id"], row["expires_at"] - now)
