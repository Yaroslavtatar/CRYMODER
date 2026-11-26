from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

router = Router(name="common")

HELP = """🛡 <b>CRYMODER</b> — бесплатный open-source модератор чатов.

<b>Модерация</b> (ответом на сообщение или по ID)
/warn [причина] — предупреждение
/unwarn — снять последнее предупреждение
/warns — список предупреждений
/resetwarns — сбросить все предупреждения
/mute [время] [причина] — мут (<code>10m</code>, <code>2h</code>, <code>1d</code>; без времени — навсегда)
/unmute — снять мут
/ban [время] [причина] — бан
/unban &lt;id&gt; — разбан
/kick — исключить (можно вернуться)
/del — удалить сообщение, на которое ответили

<b>Фильтр слов</b>
/addword &lt;слово&gt; · /delword &lt;слово&gt; · /words

<b>Настройки</b>
/settings — антифлуд, антиссылки, капча, лимит предупреждений и др.

<b>Прочее</b>
/id — ваш ID и ID чата

Добавьте бота в группу и сделайте админом с правами: удаление сообщений, блокировка участников."""


@router.message(Command("start", "help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP)


@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else "—"
    text = f"Ваш ID: <code>{user_id}</code>\nID чата: <code>{message.chat.id}</code>"
    if message.reply_to_message and message.reply_to_message.from_user:
        text += f"\nID автора ответа: <code>{message.reply_to_message.from_user.id}</code>"
    await message.reply(text)
