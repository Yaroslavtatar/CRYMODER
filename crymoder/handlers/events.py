"""События Telegram: единый конвейер групповых сообщений (учёт → защита → автомодерация → команды),
входы и выходы (с капчей), заявки, голосования, фоновые задачи."""
import asyncio
import logging
import re

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import CallbackQuery, ChatJoinRequest, ChatMemberUpdated, Message

from .. import commands
from ..config import Config
from ..core import Core
from ..utils import now, today_str
from . import automod, captcha, settings

log = logging.getLogger("crymoder.events")
router = Router(name="events")

JOINED = {"member", "restricted", "administrator", "creator"}
GONE = {"left", "kicked"}


def make_bot(token: str) -> Bot:
    return Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))


# ---------------------------------------------------------------- учёт активности
def track_message(core: Core, msg) -> None:
    chat_id, t = msg.chat.id, now()
    core.ensure_chat(chat_id, msg.chat.title or "")
    u = msg.from_user
    if not u or u.is_bot and u.id == 1087968824:  # анонимный админ
        return
    core.reg_user(u)
    core.db.execute(
        "INSERT INTO members(chat_id,user_id,first_seen,last_seen,msg_count,joined_at) VALUES(?,?,?,?,1,?)"
        " ON CONFLICT(chat_id,user_id) DO UPDATE SET last_seen=excluded.last_seen,"
        " msg_count=msg_count+1, left=0", (chat_id, u.id, t, t, t))
    core.db.execute(
        "INSERT INTO msg_daily(chat_id,user_id,day,cnt) VALUES(?,?,?,1)"
        " ON CONFLICT(chat_id,user_id,day) DO UPDATE SET cnt=cnt+1", (chat_id, u.id, today_str(t)))
    core.db.execute("INSERT OR REPLACE INTO msgs(chat_id,message_id,user_id,ts) VALUES(?,?,?,?)",
                    (chat_id, msg.message_id, u.id, t))
    if msg.message_id % 50 == 0:
        core.db.execute("DELETE FROM msgs WHERE chat_id=? AND message_id<?", (chat_id, msg.message_id - 5000))


# ---------------------------------------------------------------- защита чата
def link_violation(core: Core, msg) -> str:
    """Возвращает вид нарушения ('chats' | 'sites' | 'tags') или ''."""
    chat = msg.chat.id
    text = msg.text or msg.caption or ""
    ents = msg.entities or msg.caption_entities or []
    allowed = [r["link"] for r in core.db.query("SELECT link FROM allowed_links WHERE chat_id=?", (chat,))]
    found = []
    for e in ents:
        if e.type == "url":
            found.append(e.extract_from(text))
        elif e.type == "text_link" and e.url:
            found.append(e.url)
        elif e.type == "mention":
            found.append(e.extract_from(text))
    for raw in found:
        low = raw.lower().rstrip("/")
        bare = re.sub(r"^https?://", "", low)
        if any(bare.startswith(re.sub(r"^https?://", "", a)) for a in allowed):
            continue
        if low.startswith("@"):
            kind = "tags"
        else:
            m = re.match(r"(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/(.*)", bare)
            if m:
                path = m.group(1)
                kind = "chats" if path.startswith(("+", "joinchat", "c/")) else "tags"
            else:
                kind = "sites"
        if not core.get(chat, "allow_" + kind):
            return kind
    return ""


async def enforce(core: Core, bot: Bot, msg) -> bool:
    """True — сообщение удалено."""
    chat = msg.chat.id
    sc = msg.sender_chat
    if sc and sc.id != chat and sc.type == "channel" and not msg.is_automatic_forward \
            and not core.get(chat, "allow_channels"):
        await core._tg(bot.delete_message(chat, msg.message_id))
        await core._tg(bot.ban_chat_sender_chat(chat, sc.id))
        return True
    u = msg.from_user
    if not u or core.rank_of(chat, u.id) >= 1:
        return False
    if link_violation(core, msg):
        await core._tg(bot.delete_message(chat, msg.message_id))
        return True
    return False


# ---------------------------------------------------------------- события
@router.message(F.chat.type.in_({"group", "supergroup"}))
async def on_group_message(msg: Message, bot: Bot, core: Core):
    # служебные «вошёл / вышел»: не статистика и не команды
    if msg.new_chat_members or msg.left_chat_member:
        core.ensure_chat(msg.chat.id, msg.chat.title or "")
        if core.get(msg.chat.id, "delete_service"):
            await core._tg(msg.delete())
        return
    track_message(core, msg)
    if await enforce(core, bot, msg):            # ссылки / каналы по настройкам -ссылки, -сайты...
        return
    if await automod.check(core, bot, msg):      # антифлуд, инвайты, слова
        return
    await commands.handle_message(core, bot, msg)


@router.message(F.chat.type == "private")
async def on_private_message(msg: Message, bot: Bot, core: Core):
    if msg.from_user:
        core.reg_user(msg.from_user)
    text = (msg.text or "").strip()
    if text.lower() == "/start":
        text = "/помощь"
        msg = msg.model_copy(update={"text": text})
    await commands.handle_message(core, bot, msg)


@router.my_chat_member()
async def on_my_status(upd: ChatMemberUpdated, core: Core):
    if upd.chat.type not in ("group", "supergroup"):
        return
    if upd.new_chat_member.status in JOINED:
        core.ensure_chat(upd.chat.id, upd.chat.title or "")
        await core.sync_admins(upd.chat.id)
        if upd.new_chat_member.status != "administrator":
            await core._tg(upd.bot.send_message(
                upd.chat.id, "Привет! Сделайте меня администратором (с правами на удаление сообщений, блокировку "
                             "и приглашение участников), и я начну работать. Список команд: «помощь»."))
    elif upd.new_chat_member.status in GONE:
        core.db.execute("UPDATE chats SET active=0 WHERE chat_id=?", (upd.chat.id,))


@router.chat_member()
async def on_member_update(upd: ChatMemberUpdated, core: Core):
    bot = upd.bot
    chat_id = upd.chat.id
    user = upd.new_chat_member.user
    old, new = upd.old_chat_member.status, upd.new_chat_member.status
    was_in = old in JOINED and not (old == "restricted" and not getattr(upd.old_chat_member, "is_member", True))
    is_in = new in JOINED and not (new == "restricted" and not getattr(upd.new_chat_member, "is_member", True))
    core.ensure_chat(chat_id, upd.chat.title or "")
    core.reg_user(user)
    if "administrator" in (old, new) or "creator" in (old, new):
        await core.sync_admins(chat_id)        # назначили / сняли админа в Telegram → обновляем ранги
    if not was_in and is_in:
        await on_join(core, bot, upd, user)
    elif was_in and not is_in:
        await on_leave(core, bot, upd, user, kicked=(new == "kicked" or upd.from_user.id != user.id))


async def on_join(core: Core, bot: Bot, upd, user):
    chat_id, t = upd.chat.id, now()
    inviter = upd.from_user.id if upd.from_user and upd.from_user.id != user.id else None
    core.db.execute(
        "INSERT INTO members(chat_id,user_id,first_seen,last_seen,msg_count,joined_at,left,invited_by)"
        " VALUES(?,?,?,?,0,?,0,?) ON CONFLICT(chat_id,user_id) DO UPDATE SET left=0, joined_at=excluded.joined_at,"
        " invited_by=COALESCE(excluded.invited_by, invited_by)", (chat_id, user.id, t, t, t, inviter))
    # глобальные баны сетки
    g = core.db.scalar("SELECT grid_id FROM chats WHERE chat_id=?", (chat_id,))
    if g and core.db.one("SELECT 1 FROM gbans WHERE grid_id=? AND user_id=?", (g, user.id)):
        await core.ban(chat_id, user.id, 0, None, "глобальный бан сетки", "grid")
        return
    # бан из локального списка (на случай ручного разбана в Telegram)
    if core.db.one("SELECT 1 FROM bans WHERE chat_id=? AND user_id=? AND (until=0 OR until>?)", (chat_id, user.id, t)):
        await core.ban(chat_id, user.id, 0, None, "в банлисте", "auto")
        return
    # боты
    if user.is_bot and user.id != core.me_id and inviter and not core.get(chat_id, "allow_bots") \
            and core.rank_of(chat_id, inviter) < 2:
        await core.ban(chat_id, user.id, 0, None, "приглашение ботов запрещено", "auto")
        return
    # лимит приглашений
    lim = core.get(chat_id, "invites_limit")
    if lim and inviter and core.rank_of(chat_id, inviter) < 1:
        n = core.db.scalar("SELECT COUNT(*) FROM members WHERE chat_id=? AND invited_by=? AND joined_at>?",
                           (chat_id, inviter, t - 600), 0)
        if n > lim:
            r = await core.kick(chat_id, inviter, 0, "превышен лимит приглашений", "auto")
            if r.ok:
                await core._tg(bot.send_message(chat_id, "🚫 Превышен лимит приглашений. " + r.text))
    # капча новичкам (не ботам и не модераторам)
    if not user.is_bot and core.get(chat_id, "captcha") and core.rank_of(chat_id, user.id) < 1:
        await captcha.start(core, bot, chat_id, user)
    if core.get(chat_id, "show_joins") and not user.is_bot:
        await core._tg(bot.send_message(chat_id, f"➕ {core.m(user.id)} вошёл(а) в чат."))
    tpl = core.get(chat_id, "greeting")
    if tpl and user.id != core.me_id:
        prof = core.db.one("SELECT gender FROM profiles WHERE chat_id=? AND user_id=?", (chat_id, user.id))
        text = commands.render_greeting(tpl, core.m(user.id), (prof["gender"] if prof and prof["gender"] else "м"))
        await core._tg(bot.send_message(chat_id, text))


async def on_leave(core: Core, bot: Bot, upd, user, kicked: bool):
    chat_id, t = upd.chat.id, now()
    core.db.execute("UPDATE members SET left=1 WHERE chat_id=? AND user_id=?", (chat_id, user.id))
    await captcha.forget(core, bot, chat_id, user.id)
    if kicked:
        return
    core.db.execute("INSERT INTO leaves(chat_id,user_id,at) VALUES(?,?,?)", (chat_id, user.id, t))
    n = core.get(chat_id, "autokick_n")
    if n:
        win = core.get(chat_id, "autokick_window") * 60
        c = core.db.scalar("SELECT COUNT(*) FROM leaves WHERE chat_id=? AND user_id=? AND at>?",
                           (chat_id, user.id, t - win), 0)
        if c >= n:
            if core.get(chat_id, "autokick_action") == "ban":
                await core.ban(chat_id, user.id, 0, None, "автокик: частые выходы", "auto")
            else:
                await core._tg(bot.unban_chat_member(chat_id, user.id, only_if_banned=True))
    if core.get(chat_id, "show_leaves") and not user.is_bot:
        await core._tg(bot.send_message(chat_id, f"➖ {core.m(user.id)} покинул(а) чат."))


@router.chat_join_request()
async def on_join_request(req: ChatJoinRequest, core: Core):
    chat_id = req.chat.id
    core.ensure_chat(chat_id, req.chat.title or "")
    if not core.get(chat_id, "auto_requests"):
        return
    core.reg_user(req.from_user)
    g = core.db.scalar("SELECT grid_id FROM chats WHERE chat_id=?", (chat_id,))
    banned = core.db.one("SELECT 1 FROM bans WHERE chat_id=? AND user_id=?", (chat_id, req.from_user.id)) or \
        (g and core.db.one("SELECT 1 FROM gbans WHERE grid_id=? AND user_id=?", (g, req.from_user.id)))
    if banned:
        await core._tg(req.bot.decline_chat_join_request(chat_id, req.from_user.id))
    else:
        await core._tg(req.bot.approve_chat_join_request(chat_id, req.from_user.id))


@router.callback_query(F.data.startswith("gb:"))
async def on_vote(cq: CallbackQuery, core: Core):
    await commands.vote_callback(core, cq.bot, cq)


# ---------------------------------------------------------------- фон
async def maintenance_loop(core: Core, bot: Bot):
    tick = 0
    while True:
        try:
            await asyncio.sleep(30)
            core.maintenance()
            tick += 1
            if tick % 120 == 0:  # раз в час
                for r in core.db.query("SELECT chat_id FROM chats WHERE active=1"):
                    await core.sync_admins(r["chat_id"])
                    secs = core.get(r["chat_id"], "autokick_silent_secs")
                    if secs > 0:
                        ids = core.inactive_ids(r["chat_id"], only_silent=True, period=secs)
                        if ids:
                            await core.kick_many(r["chat_id"], ids, 0, "auto")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("maintenance failed")


def build_dispatcher(core: Core, config: Config | None = None) -> Dispatcher:
    """Один диспетчер на всё: настройки и капча (callback-кнопки), затем общий конвейер событий."""
    dp = Dispatcher(core=core, config=config)
    dp.include_routers(settings.router, captcha.router, router)
    return dp
