"""Инлайн-панель /settings: переключатели автомодерации. Данные хранятся в общей таблице настроек,
поэтому то же самое можно менять и командами, и в веб-панели."""
from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from ..core import RANGES, Core
from ..utils import fmt_duration

router = Router(name="settings")

TITLE = "⚙️ <b>Настройки автомодерации</b>\nНажимайте кнопки, чтобы переключать."

TOGGLES = [
    ("antiflood", "Антифлуд"),
    ("antilink", "Запрет ссылок"),
    ("antiinvite", "Запрет инвайт-ссылок"),
    ("badwords", "Фильтр слов"),
    ("captcha", "Капча новичкам"),
    ("delete_service", "Удалять служебные сообщения"),
]
NUMBERS = [
    ("warn_limit", "Варнов до наказания", 1),
    ("flood_limit", "Флуд: сообщений", 1),
    ("captcha_timeout", "Время на капчу, с", 30),
]
_ACTION_ORDER = ["mute", "ban", "kick"]
_ACTION_NAMES = {"mute": "мут", "ban": "бан", "kick": "кик"}
_KEYS = {k for k, _ in TOGGLES} | {k for k, _, _ in NUMBERS}


def _action_label(core: Core, chat_id: int) -> str:
    act = core.get(chat_id, "warn_action")
    label = _ACTION_NAMES.get(act, act)
    if act == "mute":
        label += " " + fmt_duration(core.get(chat_id, "warn_mute_secs") or 86400)
    return label


def build_keyboard(core: Core, chat_id: int) -> InlineKeyboardMarkup:
    rows = []
    for key, title in TOGGLES:
        mark = "✅" if core.get(chat_id, key) else "❌"
        rows.append([InlineKeyboardButton(text=f"{mark} {title}", callback_data=f"set:t:{key}")])
    for key, title, step in NUMBERS:
        rows.append([
            InlineKeyboardButton(text="➖", callback_data=f"set:n:{key}:-{step}"),
            InlineKeyboardButton(text=f"{title}: {core.get(chat_id, key)}", callback_data="set:noop"),
            InlineKeyboardButton(text="➕", callback_data=f"set:n:{key}:{step}"),
        ])
    rows.append([InlineKeyboardButton(text=f"При лимите варнов: {_action_label(core, chat_id)}",
                                      callback_data="set:c:warn_action")])
    rows.append([InlineKeyboardButton(text="Закрыть", callback_data="set:close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("set:"))
async def on_setting(call: CallbackQuery, core: Core):
    msg = call.message
    if not msg:
        return await call.answer()
    chat_id = msg.chat.id
    if not core.has_access(chat_id, call.from_user.id, "настройки"):
        return await call.answer("Недостаточно прав.", show_alert=True)
    parts = call.data.split(":")
    kind = parts[1]
    if kind == "noop":
        return await call.answer()
    if kind == "close":
        await call.answer()
        try:
            await msg.delete()
        except Exception:
            pass
        return
    key = parts[2] if len(parts) > 2 else ""
    if kind == "t" and key in _KEYS:
        core.set_setting(chat_id, key, 0 if core.get(chat_id, key) else 1, call.from_user.id, "chat")
    elif kind == "n" and key in _KEYS:
        lo, hi = RANGES.get(key, (0, 10**6))
        new = min(max(core.get(chat_id, key) + int(parts[3]), lo), hi)
        core.set_setting(chat_id, key, new, call.from_user.id, "chat")
    elif kind == "c" and key == "warn_action":
        cur = core.get(chat_id, "warn_action")
        nxt = _ACTION_ORDER[(_ACTION_ORDER.index(cur) + 1) % len(_ACTION_ORDER)] if cur in _ACTION_ORDER else "mute"
        core.set_setting(chat_id, "warn_action", nxt, call.from_user.id, "chat")
    else:
        return await call.answer()
    try:
        await msg.edit_reply_markup(reply_markup=build_keyboard(core, chat_id))
    except Exception:
        pass   # «message is not modified» и подобное
    await call.answer()
