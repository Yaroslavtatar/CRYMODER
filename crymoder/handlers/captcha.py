"""Капча для новичков: вступил → ограничен → нажал кнопку → свободен. Не нажал → кик."""
from __future__ import annotations

import asyncio
import logging
import time

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import JOIN_TRANSITION, ChatMemberUpdatedFilter
from aiogram.types import CallbackQuery, ChatMemberUpdated, InlineKeyboardButton, InlineKeyboardMarkup

from .. import actions
from ..db import Database
from ..utils import spawn, user_mention

log = logging.getLogger(__name__)
router = Router(name="captcha")

_tasks: dict[tuple[int, int], asyncio.Task] = {}


async def _expire(bot: Bot, db: Database, chat_id: int, user_id: int, message_id: int, delay: float) -> None:
    try:
        await asyncio.sleep(max(0, delay))
    except asyncio.CancelledError:
        return
    if not await db.get_captcha(chat_id, user_id):
        return  # уже прошёл капчу
    await db.del_captcha(chat_id, user_id)
    _tasks.pop((chat_id, user_id), None)
    try:
        await actions.do_kick(bot, chat_id, user_id)
    except TelegramAPIError as exc:
        log.warning("Капча: не удалось кикнуть %s в %s: %s", user_id, chat_id, exc)
    try:
        await bot.delete_message(chat_id, message_id)
    except TelegramAPIError:
        pass


def _schedule(bot: Bot, db: Database, chat_id: int, user_id: int, message_id: int, delay: float) -> None:
    key = (chat_id, user_id)
    old = _tasks.pop(key, None)
    if old:
        old.cancel()
    _tasks[key] = spawn(_expire(bot, db, chat_id, user_id, message_id, delay))


@router.chat_member(ChatMemberUpdatedFilter(JOIN_TRANSITION))
async def on_join(event: ChatMemberUpdated, bot: Bot, db: Database) -> None:
    user = event.new_chat_member.user
    if user.is_bot:
        return
    settings = await db.get_settings(event.chat.id)
    if not settings["captcha"]:
        return

    timeout = settings["captcha_timeout"]
    try:
        await actions.do_mute(bot, event.chat.id, user.id)
    except TelegramAPIError as exc:
        log.warning("Капча: нет прав ограничить %s в %s: %s", user.id, event.chat.id, exc)
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🤖 Я не бот", callback_data=f"cap:{user.id}")
    ]])
    msg = await bot.send_message(
        event.chat.id,
        f"👋 {user_mention(user)}, добро пожаловать!\n"
        f"Нажмите кнопку в течение {timeout} с, иначе вы будете исключены.",
        reply_markup=kb,
    )
    await db.add_captcha(event.chat.id, user.id, msg.message_id, int(time.time()) + timeout)
    _schedule(bot, db, event.chat.id, user.id, msg.message_id, timeout)


@router.callback_query(F.data.startswith("cap:"))
async def on_captcha_press(call: CallbackQuery, bot: Bot, db: Database) -> None:
    if not call.message:
        return await call.answer()
    user_id = int(call.data.split(":")[1])
    if call.from_user.id != user_id:
        return await call.answer("Эта кнопка не для вас.", show_alert=True)

    chat_id = call.message.chat.id
    if not await db.get_captcha(chat_id, user_id):
        return await call.answer()
    await db.del_captcha(chat_id, user_id)
    task = _tasks.pop((chat_id, user_id), None)
    if task:
        task.cancel()
    try:
        await actions.do_unmute(bot, chat_id, user_id)
    except TelegramAPIError as exc:
        log.warning("Капча: не удалось снять ограничения с %s: %s", user_id, exc)
    await call.answer("Добро пожаловать! ✅")
    try:
        await call.message.delete()
    except TelegramAPIError:
        pass


async def restore(bot: Bot, db: Database) -> None:
    """После рестарта бота перезапускаем таймеры незавершённых капч."""
    now = time.time()
    for row in await db.all_captchas():
        _schedule(bot, db, row["chat_id"], row["user_id"], row["message_id"], row["expires_at"] - now)
