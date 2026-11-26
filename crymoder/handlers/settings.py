from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from ..db import DEFAULTS, RANGES, WARN_ACTIONS, Database
from ..utils import can_restrict, is_anonymous_admin

router = Router(name="settings")
GROUP = F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP})

TOGGLES = [
    ("antiflood", "Антифлуд"),
    ("antiinvite", "Анти-инвайты"),
    ("antilink", "Анти-ссылки"),
    ("badwords", "Фильтр слов"),
    ("captcha", "Капча для новичков"),
    ("delete_service", "Удалять «вошёл/вышел»"),
]
NUMBERS = [
    ("max_warns", "Предупреждений до наказания", 1),
    ("flood_limit", "Флуд: сообщений", 1),
    ("captcha_timeout", "Время на капчу, с", 30),
]
ACTION_NAMES = {"mute": "мут 24ч", "ban": "бан", "kick": "кик"}
TITLE = "⚙️ <b>Настройки CRYMODER</b>\nНажимайте кнопки, чтобы переключать параметры."


def build_keyboard(s: dict) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"{'✅' if s[key] else '❌'} {label}", callback_data=f"set:t:{key}")]
        for key, label in TOGGLES
    ]
    for key, label, step in NUMBERS:
        rows.append([
            InlineKeyboardButton(text="➖", callback_data=f"set:n:{key}:-{step}"),
            InlineKeyboardButton(text=f"{label}: {s[key]}", callback_data="set:noop"),
            InlineKeyboardButton(text="➕", callback_data=f"set:n:{key}:{step}"),
        ])
    rows.append([
        InlineKeyboardButton(
            text=f"При лимите предупреждений: {ACTION_NAMES[s['warn_action']]}",
            callback_data="set:c:warn_action",
        )
    ])
    rows.append([InlineKeyboardButton(text="Закрыть", callback_data="set:close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(GROUP, Command("settings"))
async def cmd_settings(message: Message, bot: Bot, db: Database) -> None:
    allowed = is_anonymous_admin(message) or (
        message.from_user and await can_restrict(bot, message.chat.id, message.from_user.id)
    )
    if not allowed:
        return
    settings = await db.get_settings(message.chat.id)
    await message.answer(TITLE, reply_markup=build_keyboard(settings))


@router.callback_query(F.data.startswith("set:"))
async def on_setting(call: CallbackQuery, bot: Bot, db: Database) -> None:
    if not call.message:
        return await call.answer()
    chat_id = call.message.chat.id
    if not await can_restrict(bot, chat_id, call.from_user.id):
        return await call.answer("Только для админов.", show_alert=True)

    parts = call.data.split(":")
    action = parts[1]
    if action == "noop":
        return await call.answer()
    if action == "close":
        await call.message.delete()
        return await call.answer()

    settings = await db.get_settings(chat_id)
    key = parts[2]
    if key not in DEFAULTS:
        return await call.answer()

    if action == "t":
        await db.set_setting(chat_id, key, 0 if settings[key] else 1)
    elif action == "n" and key in RANGES:
        await db.set_setting(chat_id, key, settings[key] + int(parts[3]))
    elif action == "c" and key == "warn_action":
        nxt = WARN_ACTIONS[(WARN_ACTIONS.index(settings[key]) + 1) % len(WARN_ACTIONS)]
        await db.set_setting(chat_id, key, nxt)

    try:
        await call.message.edit_reply_markup(reply_markup=build_keyboard(await db.get_settings(chat_id)))
    except TelegramBadRequest:  # значение не изменилось (упёрлись в границу)
        pass
    await call.answer()
