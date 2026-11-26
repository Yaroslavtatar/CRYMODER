"""Общие действия модерации — используются и командами, и автомодерацией."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.types import ChatPermissions

from .config import Config
from .db import Database
from .utils import Target, fmt_duration

log = logging.getLogger(__name__)

MUTE_PERMS = ChatPermissions(can_send_messages=False)
FULL_PERMS = ChatPermissions(
    can_send_messages=True,
    can_send_audios=True,
    can_send_documents=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
    can_invite_users=True,
)
WARN_MUTE_SECONDS = 24 * 3600


def _until(seconds: int | None) -> datetime | None:
    return datetime.now(timezone.utc) + timedelta(seconds=seconds) if seconds else None


async def do_mute(bot: Bot, chat_id: int, user_id: int, seconds: int | None = None) -> None:
    await bot.restrict_chat_member(chat_id, user_id, permissions=MUTE_PERMS, until_date=_until(seconds))


async def do_unmute(bot: Bot, chat_id: int, user_id: int) -> None:
    chat = await bot.get_chat(chat_id)
    perms = (chat.permissions or FULL_PERMS).model_copy(update={"can_send_messages": True})
    await bot.restrict_chat_member(chat_id, user_id, permissions=perms)


async def do_ban(bot: Bot, chat_id: int, user_id: int, seconds: int | None = None) -> None:
    await bot.ban_chat_member(chat_id, user_id, until_date=_until(seconds))


async def do_unban(bot: Bot, chat_id: int, user_id: int) -> None:
    await bot.unban_chat_member(chat_id, user_id, only_if_banned=True)


async def do_kick(bot: Bot, chat_id: int, user_id: int) -> None:
    await bot.ban_chat_member(chat_id, user_id)
    await bot.unban_chat_member(chat_id, user_id, only_if_banned=True)


async def modlog(
    bot: Bot, db: Database, config: Config, chat_id: int, chat_title: str,
    admin_id: int, target_id: int, action: str, reason: str = "",
) -> None:
    await db.log(chat_id, admin_id, target_id, action, reason)
    if config.log_chat_id:
        text = (
            f"🛡 <b>{action}</b>\nЧат: {chat_title} (<code>{chat_id}</code>)\n"
            f"Кто: <code>{admin_id}</code> → цель: <code>{target_id}</code>"
            + (f"\nПричина: {reason}" if reason else "")
        )
        try:
            await bot.send_message(config.log_chat_id, text)
        except Exception as exc:  # лог-чат не должен ломать модерацию
            log.warning("Не удалось отправить в лог-чат: %s", exc)


async def apply_warn(
    bot: Bot, db: Database, config: Config, chat_id: int, chat_title: str,
    target: Target, reason: str, by: int,
) -> str:
    """Выдаёт предупреждение и при достижении лимита применяет наказание."""
    count = await db.add_warn(chat_id, target.id, reason)
    settings = await db.get_settings(chat_id)
    limit = settings["max_warns"]
    await modlog(bot, db, config, chat_id, chat_title, by, target.id, "warn", reason)

    suffix = f"\nПричина: {reason}" if reason else ""
    if count < limit:
        return f"⚠️ {target.mention} получает предупреждение ({count}/{limit}).{suffix}"

    action = settings["warn_action"]
    await db.reset_warns(chat_id, target.id)
    if action == "ban":
        await do_ban(bot, chat_id, target.id)
        verdict = "бан"
    elif action == "kick":
        await do_kick(bot, chat_id, target.id)
        verdict = "исключён из чата"
    else:
        await do_mute(bot, chat_id, target.id, WARN_MUTE_SECONDS)
        verdict = f"мут на {fmt_duration(WARN_MUTE_SECONDS)}"
    await modlog(bot, db, config, chat_id, chat_title, by, target.id, f"auto-{action}", "лимит предупреждений")
    return f"🚫 {target.mention}: лимит предупреждений ({count}/{limit}) → {verdict}.{suffix}"
