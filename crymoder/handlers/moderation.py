from __future__ import annotations

import re
from datetime import datetime
from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from .. import actions
from ..config import Config
from ..db import Database
from ..utils import (
    Target, can_restrict, fmt_duration, is_anonymous_admin, is_staff,
    parse_duration, resolve_target,
)

router = Router(name="moderation")
GROUP = F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP})
router.message.filter(GROUP)

NEED_TARGET = "Ответьте на сообщение пользователя или укажите его ID: <code>/{cmd} 123456789</code>"


async def _guard(message: Message, bot: Bot) -> bool:
    """Команды модерации доступны только админам с правом блокировки. Остальным — тишина."""
    if is_anonymous_admin(message):
        return True
    return bool(message.from_user and await can_restrict(bot, message.chat.id, message.from_user.id))


async def _prepare(message: Message, command: CommandObject, bot: Bot, *, allow_staff_target: bool = False):
    """Общая проверка: права, цель, защита админов. Возвращает (target, rest) либо None."""
    if not await _guard(message, bot):
        return None
    args = command.args.split() if command.args else []
    target, rest = resolve_target(message, args)
    if target is None:
        await message.reply(NEED_TARGET.format(cmd=command.command))
        return None
    if target.id == bot.id:
        await message.reply("Себя я наказывать не буду 🙂")
        return None
    if not allow_staff_target and await is_staff(bot, message.chat.id, target.id):
        await message.reply("Админов наказывать нельзя.")
        return None
    return target, rest


def _split_duration(rest: list[str]) -> tuple[int | None, str]:
    if rest:
        seconds = parse_duration(rest[0])
        if seconds is not None:
            return seconds, " ".join(rest[1:])
    return None, " ".join(rest)


def _admin_id(message: Message) -> int:
    return message.from_user.id if message.from_user else message.chat.id


async def _fail(message: Message, exc: TelegramAPIError) -> None:
    await message.reply(
        f"❌ Не получилось: <code>{escape(str(exc.message))}</code>\n"
        "Проверьте, что бот — админ с правами на удаление сообщений и блокировку участников."
    )


# ---------- предупреждения ----------
@router.message(Command("warn"))
async def cmd_warn(message: Message, command: CommandObject, bot: Bot, db: Database, config: Config) -> None:
    prepared = await _prepare(message, command, bot)
    if not prepared:
        return
    target, rest = prepared
    try:
        text = await actions.apply_warn(
            bot, db, config, message.chat.id, message.chat.title or "", target,
            " ".join(rest), _admin_id(message),
        )
    except TelegramAPIError as exc:
        return await _fail(message, exc)
    await message.answer(text)


@router.message(Command("unwarn"))
async def cmd_unwarn(message: Message, command: CommandObject, bot: Bot, db: Database) -> None:
    prepared = await _prepare(message, command, bot, allow_staff_target=True)
    if not prepared:
        return
    target, _ = prepared
    left = await db.remove_last_warn(message.chat.id, target.id)
    await message.answer(f"✅ Последнее предупреждение {target.mention} снято. Осталось: {left}.")


@router.message(Command("resetwarns"))
async def cmd_resetwarns(message: Message, command: CommandObject, bot: Bot, db: Database) -> None:
    prepared = await _prepare(message, command, bot, allow_staff_target=True)
    if not prepared:
        return
    target, _ = prepared
    await db.reset_warns(message.chat.id, target.id)
    await message.answer(f"✅ Предупреждения {target.mention} сброшены.")


@router.message(Command("warns"))
async def cmd_warns(message: Message, command: CommandObject, bot: Bot, db: Database) -> None:
    args = command.args.split() if command.args else []
    target, _ = resolve_target(message, args)
    if target is None or not await _guard(message, bot):
        # Обычный пользователь видит только свои предупреждения
        if not message.from_user:
            return
        target = Target(message.from_user.id, message.from_user.full_name)
    rows = await db.list_warns(message.chat.id, target.id)
    limit = (await db.get_settings(message.chat.id))["max_warns"]
    if not rows:
        return await message.reply(f"У {target.mention} нет предупреждений.")
    lines = [
        f"{i}. {datetime.fromtimestamp(r['created_at']):%d.%m.%Y} — {escape(r['reason']) or 'без причины'}"
        for i, r in enumerate(rows, 1)
    ]
    await message.reply(f"Предупреждения {target.mention} ({len(rows)}/{limit}):\n" + "\n".join(lines))


# ---------- мут / бан / кик ----------
@router.message(Command("mute"))
async def cmd_mute(message: Message, command: CommandObject, bot: Bot, db: Database, config: Config) -> None:
    prepared = await _prepare(message, command, bot)
    if not prepared:
        return
    target, rest = prepared
    seconds, reason = _split_duration(rest)
    try:
        await actions.do_mute(bot, message.chat.id, target.id, seconds)
    except TelegramAPIError as exc:
        return await _fail(message, exc)
    await actions.modlog(bot, db, config, message.chat.id, message.chat.title or "",
                         _admin_id(message), target.id, "mute", reason)
    await message.answer(
        f"🔇 {target.mention} в муте: {fmt_duration(seconds)}." + (f"\nПричина: {escape(reason)}" if reason else "")
    )


@router.message(Command("unmute"))
async def cmd_unmute(message: Message, command: CommandObject, bot: Bot, db: Database, config: Config) -> None:
    prepared = await _prepare(message, command, bot, allow_staff_target=True)
    if not prepared:
        return
    target, _ = prepared
    try:
        await actions.do_unmute(bot, message.chat.id, target.id)
    except TelegramAPIError as exc:
        return await _fail(message, exc)
    await actions.modlog(bot, db, config, message.chat.id, message.chat.title or "",
                         _admin_id(message), target.id, "unmute")
    await message.answer(f"🔊 {target.mention} снова может писать.")


@router.message(Command("ban"))
async def cmd_ban(message: Message, command: CommandObject, bot: Bot, db: Database, config: Config) -> None:
    prepared = await _prepare(message, command, bot)
    if not prepared:
        return
    target, rest = prepared
    seconds, reason = _split_duration(rest)
    try:
        await actions.do_ban(bot, message.chat.id, target.id, seconds)
    except TelegramAPIError as exc:
        return await _fail(message, exc)
    await actions.modlog(bot, db, config, message.chat.id, message.chat.title or "",
                         _admin_id(message), target.id, "ban", reason)
    await message.answer(
        f"⛔ {target.mention} забанен: {fmt_duration(seconds)}." + (f"\nПричина: {escape(reason)}" if reason else "")
    )


@router.message(Command("unban"))
async def cmd_unban(message: Message, command: CommandObject, bot: Bot, db: Database, config: Config) -> None:
    prepared = await _prepare(message, command, bot, allow_staff_target=True)
    if not prepared:
        return
    target, _ = prepared
    try:
        await actions.do_unban(bot, message.chat.id, target.id)
    except TelegramAPIError as exc:
        return await _fail(message, exc)
    await actions.modlog(bot, db, config, message.chat.id, message.chat.title or "",
                         _admin_id(message), target.id, "unban")
    await message.answer(f"✅ {target.mention} разбанен.")


@router.message(Command("kick"))
async def cmd_kick(message: Message, command: CommandObject, bot: Bot, db: Database, config: Config) -> None:
    prepared = await _prepare(message, command, bot)
    if not prepared:
        return
    target, rest = prepared
    try:
        await actions.do_kick(bot, message.chat.id, target.id)
    except TelegramAPIError as exc:
        return await _fail(message, exc)
    await actions.modlog(bot, db, config, message.chat.id, message.chat.title or "",
                         _admin_id(message), target.id, "kick", " ".join(rest))
    await message.answer(f"👢 {target.mention} исключён из чата.")


@router.message(Command("del"))
async def cmd_del(message: Message, bot: Bot) -> None:
    if not await _guard(message, bot) or not message.reply_to_message:
        return
    try:
        await message.reply_to_message.delete()
        await message.delete()
    except TelegramAPIError as exc:
        await _fail(message, exc)


# ---------- фильтр слов ----------
_WORD_RE = re.compile(r"^[\w\-]{2,40}$", re.UNICODE)


def normalize(text: str) -> str:
    return text.lower().replace("ё", "е")


@router.message(Command("addword"))
async def cmd_addword(message: Message, command: CommandObject, bot: Bot, db: Database) -> None:
    if not await _guard(message, bot):
        return
    word = normalize(command.args.strip()) if command.args else ""
    if not _WORD_RE.match(word):
        return await message.reply("Использование: <code>/addword слово</code> (одно слово, 2–40 символов).")
    await db.add_word(message.chat.id, word)
    await message.reply(f"✅ Слово <code>{escape(word)}</code> добавлено в фильтр.")


@router.message(Command("delword"))
async def cmd_delword(message: Message, command: CommandObject, bot: Bot, db: Database) -> None:
    if not await _guard(message, bot):
        return
    word = normalize(command.args.strip()) if command.args else ""
    if await db.del_word(message.chat.id, word):
        await message.reply(f"✅ Слово <code>{escape(word)}</code> удалено.")
    else:
        await message.reply("Такого слова в фильтре нет.")


@router.message(Command("words"))
async def cmd_words(message: Message, bot: Bot, db: Database) -> None:
    if not await _guard(message, bot):
        return
    words = await db.get_words(message.chat.id)
    text = ", ".join(f"<code>{escape(w)}</code>" for w in words) if words else "Фильтр пуст."
    await message.reply(f"🚫 Запрещённые слова:\n{text}")
