"""Все команды бота — в стиле Iris и со слеш-алиасами (/warn, /mute, /ban...). Каждая команда — функция
с декоратором @cmd. Префиксы: «!», «.», «/», «Ирис», «Ириска». Команды модераторов работают и без префикса."""
import logging
import re
import secrets

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyParameters

from . import registry
from .handlers import settings as settings_ui
from .core import DISABLED, RANK_NAMES, Core
from .registry import COMMANDS, cmd, find_command, resolve_code
from .utils import FOREVER, esc, fmt_duration, fmt_ts, fmt_until, norm, now, take_period

log = logging.getLogger("crymoder.commands")

# Фразы, которые не должны срабатывать без префикса, даже если команда для модераторов.
AMBIGUOUS = {"вернуть", "говори", "заткнуть", "причина", "пред", "админ", "модер", "снять", "повысить",
             "понизить", "позвать", "созвать", "warns", "del", "settings", "настройки", "words", "слова",
             "addword", "delword"}


class Ctx:
    def __init__(self, core: Core, bot, msg, tokens, lines, prefixed, bang, phrase):
        self.core, self.bot, self.msg = core, bot, msg
        self.tokens, self.lines, self.prefixed, self.bang, self.phrase = tokens, lines, prefixed, bang, phrase
        self.chat_id = msg.chat.id
        self.uid = msg.from_user.id if msg.from_user else 0
        self.source = "chat"

    @property
    def rank(self) -> int:
        return self.core.rank_of(self.chat_id, self.uid)

    async def reply(self, text: str, markup=None):
        kw = {}
        if getattr(self.msg, "is_topic_message", False) and getattr(self.msg, "message_thread_id", None):
            kw["message_thread_id"] = self.msg.message_thread_id
        for i in range(0, max(len(text), 1), 3800):
            chunk = text[i:i + 3800]
            await self.bot.send_message(self.chat_id, chunk,
                                        reply_parameters=ReplyParameters(message_id=self.msg.message_id,
                                                                         allow_sending_without_reply=True),
                                        reply_markup=markup if i == 0 else None, **kw)

    async def say(self, text: str):
        await self.bot.send_message(self.chat_id, text[:4000])

    async def err(self, text: str):
        await self.reply("❌ " + text)

    def real_reply(self):
        r = getattr(self.msg, "reply_to_message", None)
        if not r:
            return None
        if getattr(self.msg, "is_topic_message", False) and r.message_id == getattr(self.msg, "message_thread_id", None):
            return None
        return r

    async def targets(self, tokens=None):
        """Разбирает цели в токенах: @ник, ссылка t.me, ID; иначе — автор сообщения, на которое ответили.
        Возвращает (список user_id, оставшиеся токены)."""
        tokens = list(self.tokens if tokens is None else tokens)
        ids, rest = [], []
        for t in tokens:
            uid = await self._resolve_token(t)
            if uid:
                ids.append(uid)
            else:
                rest.append(t)
        for e in (getattr(self.msg, "entities", None) or []):
            if getattr(e, "type", "") == "text_mention" and e.user:
                self.core.reg_user(e.user)
                if e.user.id not in ids:
                    ids.append(e.user.id)
        if not ids:
            r = self.real_reply()
            if r and r.from_user:
                self.core.reg_user(r.from_user)
                ids.append(r.from_user.id)
        return ids, rest

    async def _resolve_token(self, t: str):
        t = t.strip().rstrip(",.;")
        m = re.fullmatch(r"(?:https?://)?t\.me/([A-Za-z0-9_]{4,})", t)
        name = None
        if t.startswith("@") and len(t) > 2:
            name = t[1:]
        elif m:
            name = m.group(1)
        elif re.fullmatch(r"\d{5,}", t):
            return int(t)
        else:
            m2 = re.fullmatch(r"tg://user\?id=(\d+)", t)
            return int(m2.group(1)) if m2 else None
        uid = self.core.user_by_username(name)
        if uid:
            return uid
        try:
            ch = await self.bot.get_chat("@" + name)
            if getattr(ch, "type", "") == "private":
                from types import SimpleNamespace
                self.core.reg_user(SimpleNamespace(id=ch.id, username=ch.username, first_name=ch.first_name,
                                                   last_name=ch.last_name, is_bot=False))
                return ch.id
        except Exception:
            pass
        return None

    def reason(self, leftover=None) -> str:
        if len(self.lines) > 1:
            return " ".join(x.strip() for x in self.lines[1:] if x.strip())[:300]
        return " ".join(leftover or [])[:300]

    def text_arg(self) -> str:
        """Текст команды: остаток первой строки + следующие строки."""
        first = " ".join(self.tokens)
        rest = "\n".join(self.lines[1:])
        return (first + ("\n" if first and rest else "") + rest).strip()


# ============================================================ модерация

@cmd("модер", "Назначение рангов", "Модерация", 4,
     ["+модер", "+админ", "!модер", "модер", "админ", "повысить", "понизить"], log=True)
async def h_rank(ctx: Ctx):
    toks = list(ctx.tokens)
    n = None
    if toks and re.fullmatch(r"[1-5]", toks[0]):
        n, toks = int(toks[0]), toks[1:]
    ids, _ = await ctx.targets(toks)
    if not ids:
        return await ctx.err("Укажите пользователя: ответом на сообщение, @username или ID.")
    for t in ids:
        cur = ctx.core.rank_of(ctx.chat_id, t)
        if ctx.phrase == "повысить":
            new = cur + 1 if n is None else n
        elif ctx.phrase == "понизить":
            new = max(cur - 1, 0)
        else:
            new = n or ctx.bang or 1
        r = ctx.core.set_rank(ctx.chat_id, t, new, ctx.uid)
        await ctx.reply(("✅ " if r.ok else "❌ ") + r.text)


@cmd("снять", "Снятие модератора", "Модерация", 4,
     ["-модер", "-админ", "снять", "разжаловать", "снять вышедших"], log=True)
async def h_unrank(ctx: Ctx):
    if ctx.phrase == "снять вышедших":
        n = 0
        for r in ctx.core.staff(ctx.chat_id):
            left = ctx.core.db.scalar("SELECT left FROM members WHERE chat_id=? AND user_id=?",
                                      (ctx.chat_id, r["user_id"]), 0)
            if left and ctx.core.rank_of(ctx.chat_id, ctx.uid) > r["rank"]:
                ctx.core.set_rank(ctx.chat_id, r["user_id"], 0, ctx.uid)
                n += 1
        return await ctx.reply(f"✅ Снято вышедших модераторов: {n}.")
    ids, _ = await ctx.targets()
    if not ids:
        return await ctx.err("Укажите пользователя.")
    for t in ids:
        r = ctx.core.set_rank(ctx.chat_id, t, 0, ctx.uid)
        await ctx.reply(("✅ " if r.ok else "❌ ") + r.text)


@cmd("снять всех", "Снять всех модераторов", "Модерация", 5, ["снять всех", "разжаловать всех"], log=True)
async def h_unrank_all(ctx: Ctx):
    for r in ctx.core.staff(ctx.chat_id):
        ctx.core._put_rank(ctx.chat_id, r["user_id"], 0, ctx.uid)
    ctx.core.audit(ctx.chat_id, ctx.uid, "снять всех", "", "chat")
    await ctx.reply("✅ Статус снят со всех модераторов. Создатель может вернуть права командой «восстановить создателя».")


@cmd("отставка", "Снять с себя полномочия", "Модерация", 1,
     ["снимаю полномочия", "ухожу в отставку", "разжаловать меня"], log=True)
async def h_resign(ctx: Ctx):
    ctx.core._put_rank(ctx.chat_id, ctx.uid, 0, ctx.uid)
    await ctx.reply("✅ Вы сняли с себя полномочия.")


@cmd("восстановить создателя", "Восстановить права создателя", "Модерация", 0,
     ["восстановить создателя", "восстановить владельца", "восстановить права", "хозяин вернулся", "хв"], log=True)
async def h_restore(ctx: Ctx):
    await ctx.core.sync_creator(ctx.chat_id)
    if ctx.core.creator_of(ctx.chat_id) != ctx.uid:
        return await ctx.err("Это может сделать только создатель чата в Telegram.")
    ctx.core._put_rank(ctx.chat_id, ctx.uid, 5, 0)
    await ctx.reply("👑 Права создателя восстановлены (5 ранг).")


@cmd("передать создателя", "Передать права создателя", "Модерация", 5, ["передать создателя"], log=True)
async def h_transfer(ctx: Ctx):
    ids, _ = await ctx.targets()
    if not ids:
        return await ctx.err("Укажите пользователя.")
    ctx.core._put_rank(ctx.chat_id, ids[0], 5, ctx.uid)
    await ctx.reply(f"👑 {ctx.core.m(ids[0])} получает 5 ранг. Права создателя в самом Telegram нужно "
                    f"передать отдельно; затем новый создатель вводит «восстановить создателя».")


@cmd("кто админ", "Список модераторов", "Модерация", 0,
     ["кто админ", "а судьи кто", "кто здесь власть", "staff", "управляющие", "админы"])
async def h_staff(ctx: Ctx):
    rows = ctx.core.staff(ctx.chat_id)
    if not rows:
        return await ctx.reply("В чате пока нет модераторов.")
    out, cur = ["⭐️ <b>Модераторы чата</b>"], None
    for r in rows:
        if r["rank"] != cur:
            cur = r["rank"]
            out.append(f"\n<b>{esc(RANK_NAMES[cur].capitalize())}</b> ({cur})")
        left = ctx.core.db.scalar("SELECT left FROM members WHERE chat_id=? AND user_id=?",
                                  (ctx.chat_id, r["user_id"]), 0)
        out.append(f"{'➖' if left else '•'} {ctx.core.m(r['user_id'])}")
    await ctx.reply("\n".join(out))


@cmd("кто назначил", "Кто назначил модератора", "Модерация", 0, ["кто назначил", "роль"])
async def h_who_appointed(ctx: Ctx):
    ids, _ = await ctx.targets()
    if not ids:
        return await ctx.err("Укажите пользователя.")
    r = ctx.core.db.one("SELECT * FROM ranks WHERE chat_id=? AND user_id=?", (ctx.chat_id, ids[0]))
    if not r:
        return await ctx.reply("Этот пользователь не модератор.")
    by = ctx.core.m(r["appointed_by"]) if r["appointed_by"] else "система"
    await ctx.reply(f"{ctx.core.m(ids[0])} — {esc(RANK_NAMES[r['rank']])}. Назначил: {by}, {fmt_ts(r['at'])}.")


@cmd("модер лог", "Лог изменений рангов", "Модерация", 1, ["модер лог", "мой модер лог", "твой модер лог"])
async def h_rank_log(ctx: Ctx):
    sql, args = "SELECT * FROM rank_log WHERE chat_id=?", [ctx.chat_id]
    if ctx.phrase == "мой модер лог":
        sql += " AND user_id=?"
        args.append(ctx.uid)
    elif ctx.phrase == "твой модер лог":
        ids, _ = await ctx.targets()
        if not ids:
            return await ctx.err("Укажите пользователя.")
        sql += " AND user_id=?"
        args.append(ids[0])
    rows = ctx.core.db.query(sql + " ORDER BY id DESC LIMIT 20", args)
    if not rows:
        return await ctx.reply("Изменений рангов нет.")
    await ctx.reply("📜 <b>Лог рангов</b>\n" + "\n".join(
        f"{fmt_ts(r['at'])}: {ctx.core.m(r['user_id'])} {r['old']}→{r['new']} (от "
        f"{ctx.core.m(r['by_id']) if r['by_id'] else 'системы'})" for r in rows))


async def _mention_chunks(ctx: Ctx, ids, header=""):
    ids = list(ids)
    if not ids:
        return await ctx.reply("Некого звать.")
    for i in range(0, len(ids), 20):
        part = " ".join(f'<a href="tg://user?id={u}">⁠</a>{esc(ctx.core.name(u))}' for u in ids[i:i + 20])
        await ctx.say((header + "\n" if header and i == 0 else "") + part)


@cmd("созвать модеров", "Созыв модерации", "Модерация", 1,
     ["созвать модеров", "позвать админов", "позвать модеров", "созвать админов"])
async def h_call_mods(ctx: Ctx):
    await _mention_chunks(ctx, [r["user_id"] for r in ctx.core.staff(ctx.chat_id)][:20], "📣 Модерация, вас зовут:")


# ============================================================ предупреждения

async def _target_period_reason(ctx: Ctx):
    ids, rest = await ctx.targets()
    secs, rest = take_period(rest)
    if secs is None and ids:
        # период может стоять после цели: «варн @user 2 часа» — цель уже вынута, период в rest
        pass
    return ids, secs, ctx.reason(rest)


async def _need_targets(ctx: Ctx, ids):
    if not ids:
        if ctx.prefixed:
            await ctx.err("Укажите пользователя: ответом на сообщение, @username или ID.")
        return False
    return True


@cmd("варн", "Выдача предупреждения", "Наказания", 1, ["варн", "пред", "предупреждение", "warn"], log=True)
async def h_warn(ctx: Ctx):
    ids, secs, reason = await _target_period_reason(ctx)
    if not await _need_targets(ctx, ids):
        return
    for t in ids:
        r = await ctx.core.warn(ctx.chat_id, t, ctx.uid, secs, reason)
        await ctx.reply(r.text if r.ok else "❌ " + r.text)


@cmd("снять варн", "Снятие предупреждений", "Наказания", 1,
     ["-варн", "снять варн", "снять варны", "снять все варны", "снять варн номер", "unwarn", "resetwarns"],
     log=True)
async def h_unwarn(ctx: Ctx):
    ids, rest = await ctx.targets()
    if not await _need_targets(ctx, ids):
        return
    num = next((int(x) for x in rest if x.isdigit()), None)
    for t in ids:
        if ctx.phrase in ("снять все варны", "resetwarns"):
            r = ctx.core.unwarn(ctx.chat_id, t, ctx.uid, all_=True)
        elif ctx.phrase == "снять варн номер":
            r = ctx.core.unwarn(ctx.chat_id, t, ctx.uid, number=num or 1)
        else:
            r = ctx.core.unwarn(ctx.chat_id, t, ctx.uid, count=num or 1)
        await ctx.reply(r.text if r.ok else "❌ " + r.text)


def _warn_lines(core: Core, rows):
    out = []
    for i, w in enumerate(rows, 1):
        until = "" if not w["expires_at"] else f" (до {fmt_ts(w['expires_at'])})"
        out.append(f"{i}. {fmt_ts(w['created_at'])}{until} от {core.m(w['by_id']) if w['by_id'] else 'системы'}"
                   + (f" — {esc(w['reason'])}" if w["reason"] else ""))
    return out


@cmd("варны", "Просмотр предупреждений участника", "Наказания", 1, ["варны"])
async def h_warns(ctx: Ctx):
    ids, _ = await ctx.targets()
    if not await _need_targets(ctx, ids):
        return
    rows = ctx.core.active_warns(ctx.chat_id, ids[0])
    lim = ctx.core.get(ctx.chat_id, "warn_limit")
    if not rows:
        return await ctx.reply(f"У {ctx.core.m(ids[0])} нет предупреждений.")
    await ctx.reply(f"⚠️ Предупреждения {ctx.core.m(ids[0])} ({len(rows)}/{lim}):\n" + "\n".join(_warn_lines(ctx.core, rows)))


@cmd("мои варны", "Мои предупреждения", "Наказания", 0, ["мои варны", "мои преды", "warns"])
async def h_my_warns(ctx: Ctx):
    target = ctx.uid
    if ctx.rank >= 1:                       # модератор может посмотреть чужие: /warns в ответ на сообщение
        ids, _ = await ctx.targets()
        if ids:
            target = ids[0]
    rows = ctx.core.active_warns(ctx.chat_id, target)
    lim = ctx.core.get(ctx.chat_id, "warn_limit")
    if target != ctx.uid:
        if not rows:
            return await ctx.reply(f"У {ctx.core.m(target)} нет предупреждений.")
        return await ctx.reply(f"⚠️ Предупреждения {ctx.core.m(target)} ({len(rows)}/{lim}):\n"
                               + "\n".join(_warn_lines(ctx.core, rows)))
    if not rows:
        return await ctx.reply("У вас нет предупреждений. 👍")
    await ctx.reply(f"⚠️ Ваши предупреждения ({len(rows)}/{lim}):\n" + "\n".join(_warn_lines(ctx.core, rows)))


@cmd("варнлист", "Список предупреждений чата", "Наказания", 1, ["варнлист"])
async def h_warnlist(ctx: Ctx):
    rows = ctx.core.db.query(
        "SELECT user_id, COUNT(*) c FROM warns WHERE chat_id=? AND (expires_at=0 OR expires_at>?)"
        " GROUP BY user_id ORDER BY c DESC LIMIT 50", (ctx.chat_id, now()))
    if not rows:
        return await ctx.reply("Активных предупреждений в чате нет.")
    await ctx.reply("⚠️ <b>Варнлист</b>\n" + "\n".join(f"{ctx.core.m(r['user_id'])} — {r['c']}" for r in rows))


_WARN_ACTIONS = {"мут": "mute", "mute": "mute", "бан": "ban", "ban": "ban", "кик": "kick", "kick": "kick"}


@cmd("варны настройка", "Настройка предупреждений", "Наказания", 3,
     ["варны лимит", "варны чс", "варны период", "варны действие", "варны мут"], log=True)
async def h_warn_cfg(ctx: Ctx):
    if ctx.phrase == "варны лимит":
        if not ctx.tokens or not ctx.tokens[0].isdigit():
            return await ctx.err("Формат: варны лимит {число}")
        r = ctx.core.set_setting(ctx.chat_id, "warn_limit", ctx.tokens[0], ctx.uid, "chat")
        return await ctx.reply(("✅ Лимит варнов: " + ctx.tokens[0]) if r.ok else "❌ " + r.text)
    if ctx.phrase == "варны действие":
        act = _WARN_ACTIONS.get(ctx.tokens[0].lower()) if ctx.tokens else None
        if not act:
            return await ctx.err("Формат: варны действие мут|бан|кик")
        ctx.core.set_setting(ctx.chat_id, "warn_action", act, ctx.uid, "chat")
        return await ctx.reply(f"✅ При лимите варнов: {ctx.tokens[0].lower()}.")
    secs, _ = take_period(ctx.tokens)
    if secs is None:
        return await ctx.err("Укажите период, например: 3 дня")
    if ctx.phrase == "варны чс":              # бан на срок при лимите варнов
        ctx.core.put(ctx.chat_id, "warn_action", "ban")
        ctx.core.put(ctx.chat_id, "warn_ban_secs", 0 if secs == FOREVER else secs)
    elif ctx.phrase == "варны мут":           # мут на срок при лимите варнов
        ctx.core.put(ctx.chat_id, "warn_action", "mute")
        ctx.core.put(ctx.chat_id, "warn_mute_secs", secs)
    else:
        ctx.core.put(ctx.chat_id, "warn_ttl_secs", 0 if secs == FOREVER else secs)
    await ctx.reply(f"✅ Установлено: {fmt_duration(secs)}.")


# ============================================================ мут

@cmd("мут", "Выдача мута", "Наказания", 1, ["мут", "mute", "заткнуть"], log=True)
async def h_mute(ctx: Ctx):
    ids, secs, reason = await _target_period_reason(ctx)
    if not await _need_targets(ctx, ids):
        return
    for t in ids:
        r = await ctx.core.mute(ctx.chat_id, t, ctx.uid, secs, reason)
        await ctx.reply(r.text if r.ok else "❌ " + r.text)


@cmd("снять мут", "Снятие мута", "Наказания", 1, ["-мут", "снять мут", "говори", "unmute", "размут"], log=True)
async def h_unmute(ctx: Ctx):
    ids, _ = await ctx.targets()
    if not await _need_targets(ctx, ids):
        return
    for t in ids:
        r = await ctx.core.unmute(ctx.chat_id, t, ctx.uid)
        await ctx.reply(r.text if r.ok else "❌ " + r.text)


@cmd("муты", "Список замутенных", "Наказания", 1, ["муты"])
async def h_mutes(ctx: Ctx):
    rows = ctx.core.db.query("SELECT * FROM mutes WHERE chat_id=? ORDER BY created_at DESC LIMIT 60", (ctx.chat_id,))
    if not rows:
        return await ctx.reply("Замутенных нет.")
    await ctx.reply("🔇 <b>Муты</b>\n" + "\n".join(
        f"{ctx.core.m(r['user_id'])} — {fmt_until(r['until'])}" + (f" ({esc(r['reason'])})" if r["reason"] else "")
        for r in rows))


@cmd("проверить мут", "Проверка мута", "Наказания", 1, ["проверить мут"])
async def h_check_mute(ctx: Ctx):
    ids, _ = await ctx.targets()
    if not await _need_targets(ctx, ids):
        return
    r = ctx.core.db.one("SELECT * FROM mutes WHERE chat_id=? AND user_id=?", (ctx.chat_id, ids[0]))
    await ctx.reply(f"🔇 {ctx.core.m(ids[0])} в муте {fmt_until(r['until'])}." if r
                    else f"{ctx.core.m(ids[0])} не в муте.")


@cmd("мут период", "Срок мута по умолчанию", "Наказания", 3, ["мут период"], log=True)
async def h_mute_period(ctx: Ctx):
    secs, _ = take_period(ctx.tokens)
    if secs is None or secs < 60:
        return await ctx.err("Укажите период (минимум 1 минута), например: 2 часа")
    ctx.core.put(ctx.chat_id, "mute_default_secs", secs)
    await ctx.reply(f"✅ Мут по умолчанию: {fmt_duration(secs)}.")


# ============================================================ бан / кик

@cmd("бан", "Бан", "Наказания", 2, ["бан", "чс", "ban", "permban"], log=True)
async def h_ban(ctx: Ctx):
    ids, secs, reason = await _target_period_reason(ctx)
    if not await _need_targets(ctx, ids):
        return
    for t in ids:
        r = await ctx.core.ban(ctx.chat_id, t, ctx.uid, secs, reason)
        await ctx.reply(r.text if r.ok else "❌ " + r.text)


@cmd("разбан", "Разбан", "Наказания", 2, ["разбан", "вернуть", "unban"], log=True)
async def h_unban(ctx: Ctx):
    ids, _ = await ctx.targets()
    if not await _need_targets(ctx, ids):
        return
    for t in ids:
        r = await ctx.core.unban(ctx.chat_id, t, ctx.uid)
        await ctx.reply(r.text if r.ok else "❌ " + r.text)


@cmd("причина", "Причина бана", "Наказания", 1, ["причина"])
async def h_reason(ctx: Ctx):
    ids, _ = await ctx.targets()
    if not await _need_targets(ctx, ids):
        return
    r = ctx.core.db.one("SELECT * FROM bans WHERE chat_id=? AND user_id=?", (ctx.chat_id, ids[0]))
    if not r:
        return await ctx.reply("Бана у пользователя нет.")
    await ctx.reply(f"🚫 {ctx.core.m(ids[0])}: {esc(r['reason'] or 'причина не указана')}. "
                    f"Выдал: {ctx.core.m(r['by_id']) if r['by_id'] else 'система'}, {fmt_ts(r['created_at'])}.")


@cmd("кик", "Кик", "Наказания", 2, ["кик", "kick", "кик тихо"], log=True)
async def h_kick(ctx: Ctx):
    ids, rest = await ctx.targets()
    if not await _need_targets(ctx, ids):
        return
    silent = ctx.phrase == "кик тихо"
    for t in ids:
        r = await ctx.core.kick(ctx.chat_id, t, ctx.uid, ctx.reason(rest))
        if not silent or not r.ok:
            await ctx.reply(r.text if r.ok else "❌ " + r.text)
    if silent:
        await ctx.core._tg(ctx.bot.delete_message(ctx.chat_id, ctx.msg.message_id))


@cmd("бан период", "Срок бана по умолчанию", "Наказания", 3, ["бан период"], log=True)
async def h_ban_period(ctx: Ctx):
    secs, _ = take_period(ctx.tokens)
    if secs is None:
        return await ctx.err("Укажите период, например: 1 месяц или навсегда")
    ctx.core.put(ctx.chat_id, "ban_default_secs", 0 if secs == FOREVER else secs)
    await ctx.reply(f"✅ Бан по умолчанию: {fmt_duration(secs)}.")


@cmd("амнистия", "Амнистия", "Наказания", 4, ["амнистия"], log=True)
async def h_amnesty(ctx: Ctx):
    r = await ctx.core.amnesty(ctx.chat_id, ctx.uid)
    await ctx.reply(r.text)


@cmd("банлист", "Список банов", "Наказания", 1, ["банлист"])
async def h_banlist(ctx: Ctx):
    rows = ctx.core.db.query("SELECT * FROM bans WHERE chat_id=? ORDER BY created_at DESC LIMIT 60", (ctx.chat_id,))
    if not rows:
        return await ctx.reply("Банлист пуст.")
    await ctx.reply("🚫 <b>Банлист</b>\n" + "\n".join(
        f"{ctx.core.m(r['user_id'])} — {fmt_until(r['until'])}" + (f" ({esc(r['reason'])})" if r["reason"] else "")
        for r in rows))


# ============================================================ голосование за бан

@cmd("гб начать", "Голосование за бан", "Голосования", 0, ["гб"], log=True)
async def h_vote_ban(ctx: Ctx):
    ids, rest = await ctx.targets()
    if not await _need_targets(ctx, ids):
        return
    t = ids[0]
    need, minr = 5, 0
    nums = [int(x) for x in rest if x.isdigit()]
    if len(nums) >= 1:
        need = max(2, min(nums[0], 100))
    if len(nums) >= 2:
        minr = max(0, min(nums[1], 5))
    if ctx.core.rank_of(ctx.chat_id, t) >= 1 or t == ctx.core.me_id:
        return await ctx.err("За бан модератора проголосовать нельзя.")
    if ctx.core.db.one("SELECT 1 FROM votes WHERE chat_id=? AND target_id=? AND closed=0", (ctx.chat_id, t)):
        return await ctx.err("Голосование за этого пользователя уже идёт.")
    vid = ctx.core.db.execute(
        "INSERT INTO votes(chat_id,target_id,need,min_rank,by_id,created_at) VALUES(?,?,?,?,?,?)",
        (ctx.chat_id, t, need, minr, ctx.uid, now()))
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"🚫 За бан (0/{need})",
                                                                      callback_data=f"gb:{vid}")]])
    await ctx.reply(f"🗳 Голосование за бан {ctx.core.m(t)}. Нужно голосов: {need}"
                    + (f" (ранг от {minr})" if minr else "") + ".", markup=kb)


@cmd("гб список", "Список голосований за бан", "Голосования", 0, ["гб список"])
async def h_vote_list(ctx: Ctx):
    rows = ctx.core.db.query("SELECT * FROM votes WHERE chat_id=? AND closed=0", (ctx.chat_id,))
    if not rows:
        return await ctx.reply("Активных голосований нет.")
    await ctx.reply("🗳 <b>Голосования</b>\n" + "\n".join(
        f"#{r['id']} {ctx.core.m(r['target_id'])} — "
        f"{ctx.core.db.scalar('SELECT COUNT(*) FROM vote_ballots WHERE vote_id=?', (r['id'],), 0)}/{r['need']}"
        for r in rows))


@cmd("гб стоп", "Остановить голосование за бан", "Голосования", 2, ["гб стоп"], log=True)
async def h_vote_stop(ctx: Ctx):
    ids, _ = await ctx.targets()
    if not await _need_targets(ctx, ids):
        return
    ctx.core.db.execute("UPDATE votes SET closed=1 WHERE chat_id=? AND target_id=? AND closed=0",
                        (ctx.chat_id, ids[0]))
    await ctx.reply("✅ Голосование остановлено.")


async def vote_callback(core: Core, bot, cq):
    try:
        vid = int(cq.data.split(":")[1])
    except Exception:
        return
    v = core.db.one("SELECT * FROM votes WHERE id=?", (vid,))
    if not v or v["closed"]:
        return await cq.answer("Голосование завершено.", show_alert=True)
    uid = cq.from_user.id
    core.reg_user(cq.from_user)
    if uid == v["target_id"]:
        return await cq.answer("За себя голосовать нельзя.", show_alert=True)
    if core.rank_of(v["chat_id"], uid) < v["min_rank"]:
        return await cq.answer(f"Нужен ранг не ниже {v['min_rank']}.", show_alert=True)
    if core.db.one("SELECT 1 FROM vote_ballots WHERE vote_id=? AND user_id=?", (vid, uid)):
        return await cq.answer("Вы уже проголосовали.")
    core.db.execute("INSERT INTO vote_ballots(vote_id,user_id) VALUES(?,?)", (vid, uid))
    n = core.db.scalar("SELECT COUNT(*) FROM vote_ballots WHERE vote_id=?", (vid,), 0)
    await cq.answer("Голос принят.")
    if n >= v["need"]:
        core.db.execute("UPDATE votes SET closed=1 WHERE id=?", (vid,))
        r = await core.ban(v["chat_id"], v["target_id"], 0, None, "голосование участников", "vote")
        text = f"🗳 Голосование завершено. {r.text if r.ok else 'Не удалось: ' + r.text}"
        try:
            await bot.edit_message_text(text, chat_id=cq.message.chat.id, message_id=cq.message.message_id)
        except Exception:
            await bot.send_message(v["chat_id"], text)
    else:
        try:
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                text=f"🚫 За бан ({n}/{v['need']})", callback_data=f"gb:{vid}")]])
            await bot.edit_message_reply_markup(chat_id=cq.message.chat.id, message_id=cq.message.message_id,
                                                reply_markup=kb)
        except Exception:
            pass


# ============================================================ доступ команд

def _access_text(core: Core, chat_id: int, uid=None) -> str:
    out, section = ["⚙️ <b>Доступ команд</b>  (✅ ранг / ❌ отключена)"], None
    for c in COMMANDS.values():
        if uid is not None and not core.has_access(chat_id, uid, c.code):
            mark = "❌"
        elif uid is None:
            mark = "❌" if core.required_rank(chat_id, c.code) >= DISABLED else "✅"
        else:
            mark = "✅"
        if c.section != section:
            section = c.section
            out.append(f"\n▶️ <b>{esc(section)}</b>")
        req = core.required_rank(chat_id, c.code)
        out.append(f"{mark} {c.number}. {esc(c.code)} ({'выкл' if req >= DISABLED else req})" if uid is None
                   else f"{mark} {c.number}. {esc(c.code)}")
    return "\n".join(out)


@cmd("доступ команд", "Просмотр доступа команд", "Доступ команд", 1, ["доступ команд", "дк"], pm=False)
async def h_dk_view(ctx: Ctx):
    toks = [t for t in ctx.tokens]
    if not toks:
        return await ctx.reply(_access_text(ctx.core, ctx.chat_id))
    return await _dk_set(ctx, toks)


async def _dk_set(ctx: Ctx, toks, rank_override=None):
    if not ctx.core.has_access(ctx.chat_id, ctx.uid, "дк"):
        return await ctx.err("Недостаточно прав для изменения доступа.")
    rank = rank_override
    if rank is None:
        if toks and toks[-1].isdigit() and not (len(toks) == 1):
            rank, toks = int(toks[-1]), toks[:-1]
        else:
            return await ctx.err("Формат: дк {команда или номер} {ранг 0–5, 6 — выключить}")
    c = resolve_code(" ".join(toks))
    if not c:
        return await ctx.err("Такой команды нет. Список: «доступ команд».")
    if c.code in ("дк", "доступ команд") and rank >= DISABLED:
        return await ctx.err("Команды управления доступом отключить нельзя.")
    r = ctx.core.set_access(ctx.chat_id, c.code, rank, ctx.uid)
    await ctx.reply(("✅ " if r.ok else "❌ ") + r.text)


@cmd("дк", "Изменение доступа команд", "Доступ команд", 4, ["+дк", "-дк"], log=True)
async def h_dk_edit(ctx: Ctx):
    if not ctx.tokens:
        return await ctx.err("Укажите команду.")
    await _dk_set(ctx, ctx.tokens, rank_override=0 if ctx.phrase == "+дк" else DISABLED)


@cmd("мой дк", "Мой доступ команд", "Доступ команд", 0, ["мой доступ команд", "мдк", "мой дк"])
async def h_my_dk(ctx: Ctx):
    await ctx.reply(_access_text(ctx.core, ctx.chat_id, ctx.uid))


@cmd("сброс дк", "Сброс доступа команд", "Доступ команд", 5, ["сброс команд", "сброс дк"], log=True)
async def h_dk_reset(ctx: Ctx):
    if ctx.phrase == "сброс дк" and ctx.tokens:
        c = resolve_code(" ".join(ctx.tokens))
        if not c:
            return await ctx.err("Такой команды нет.")
        r = ctx.core.reset_access(ctx.chat_id, ctx.uid, c.code)
    else:
        r = ctx.core.reset_access(ctx.chat_id, ctx.uid)
    await ctx.reply("✅ " + r.text)


@cmd("лдк", "Личный доступ команд", "Доступ команд", 4, ["лдк", "+лдк", "-лдк", "все лдк", "сброс лдк"], log=True)
async def h_ldk(ctx: Ctx):
    ph = ctx.phrase
    if ph == "все лдк":
        rows = ctx.core.db.query("SELECT * FROM cmd_access_user WHERE chat_id=? LIMIT 80", (ctx.chat_id,))
        return await ctx.reply("👤 <b>Личные доступы</b>\n" + ("\n".join(
            f"{ctx.core.m(r['user_id'])} — {esc(r['code'])}: {'✅' if r['allowed'] else '❌'}" for r in rows)
            or "нет исключений"))
    ids, rest = await ctx.targets()
    if not await _need_targets(ctx, ids):
        return
    if ph == "лдк" or ph == "сброс лдк" and not rest:
        if ph == "сброс лдк":
            ctx.core.db.execute("DELETE FROM cmd_access_user WHERE chat_id=? AND user_id=?", (ctx.chat_id, ids[0]))
            return await ctx.reply("✅ Исключения сброшены.")
        rows = ctx.core.db.query("SELECT * FROM cmd_access_user WHERE chat_id=? AND user_id=?", (ctx.chat_id, ids[0]))
        return await ctx.reply(f"👤 Исключения {ctx.core.m(ids[0])}:\n" + ("\n".join(
            f"{esc(r['code'])}: {'✅' if r['allowed'] else '❌'}" for r in rows) or "нет"))
    c = resolve_code(" ".join(rest))
    if not c:
        return await ctx.err("Укажите команду: +лдк {команда} {ссылка}")
    r = ctx.core.set_personal(ctx.chat_id, ids[0], c.code, 1 if ph == "+лдк" else 0, ctx.uid)
    await ctx.reply(("✅ " if r.ok else "❌ ") + r.text)


@cmd("лог дк", "Лог изменений доступа", "Доступ команд", 2, ["лог дк"])
async def h_dk_log(ctx: Ctx):
    rows = ctx.core.db.query("SELECT * FROM access_log WHERE chat_id=? ORDER BY id DESC LIMIT 20", (ctx.chat_id,))
    await ctx.reply("📜 <b>Лог дк</b>\n" + ("\n".join(
        f"{fmt_ts(r['at'])}: {esc(r['code'])} {r['old']}→{r['new']} ({ctx.core.m(r['by_id'])})" for r in rows)
        or "изменений нет"))


# ============================================================ чистка

def _recent_ids(core: Core, chat_id: int, below: int, count: int, above: int = 0):
    rows = core.db.query(
        "SELECT message_id FROM msgs WHERE chat_id=? AND message_id<? AND message_id>? "
        "ORDER BY message_id DESC LIMIT ?", (chat_id, below, above, count))
    return [r["message_id"] for r in rows]


async def _delete_ids(ctx: Ctx, ids):
    n = 0
    for i in range(0, len(ids), 100):
        ok, _ = await ctx.core._tg(ctx.bot.delete_messages(ctx.chat_id, ids[i:i + 100]))
        if ok:
            n += len(ids[i:i + 100])
    return n


@cmd("-смс", "Удаление сообщений", "Чистка чата", 2, ["-смс", "del"], log=True)
async def h_del(ctx: Ctx):
    toks = list(ctx.tokens)
    silent = "тихо" in toks
    nums = [int(x) for x in toks if x.isdigit()]
    r = ctx.real_reply()
    if not nums:
        if not r:
            return await ctx.err("Ответьте на сообщение или укажите число: -смс 10")
        ids = [r.message_id, ctx.msg.message_id]
    else:
        n = min(nums[0], 1000)
        ids = _recent_ids(ctx.core, ctx.chat_id, ctx.msg.message_id, n) + [ctx.msg.message_id]
    done = await _delete_ids(ctx, ids)
    ctx.core.audit(ctx.chat_id, ctx.uid, "-смс", f"{done}", "chat")
    if not silent and len(ids) > 2:
        await ctx.say(f"🧹 Удалено сообщений: {max(done - 1, 0)}.")


@cmd("пург", "Удаление сообщений ниже отмеченного", "Чистка чата", 2, ["пург"], log=True)
async def h_purge(ctx: Ctx):
    r = ctx.real_reply()
    if not r:
        return await ctx.err("Пишите «пург» ответом на сообщение.")
    nums = [int(x) for x in ctx.tokens if x.isdigit()]
    ids = _recent_ids(ctx.core, ctx.chat_id, ctx.msg.message_id, nums[0] if nums else 1000, above=r.message_id - 1)
    ids = sorted(set(ids + [ctx.msg.message_id]))
    if nums:
        ids = ids[:nums[0]] + [ctx.msg.message_id]
    done = await _delete_ids(ctx, list(set(ids)))
    if "тихо" not in ctx.tokens:
        await ctx.say(f"🧹 Удалено сообщений: {max(done - 1, 0)}.")


@cmd("кик неактив", "Кик неактивных", "Чистка чата", 3, ["кик неактив"], log=True)
async def h_kick_inactive(ctx: Ctx):
    secs, rest = take_period(ctx.tokens)
    if secs is not None:
        if secs < 86400:
            return await ctx.err("Минимальный период — 24 часа.")
        ids = ctx.core.inactive_ids(ctx.chat_id, secs=secs)
    elif ctx.tokens and ctx.tokens[0].isdigit():
        ids = ctx.core.inactive_ids(ctx.chat_id, count=int(ctx.tokens[0]))
    else:
        return await ctx.err("Формат: кик неактив 2 месяца  или  кик неактив 10")
    r = await ctx.core.kick_many(ctx.chat_id, ids, ctx.uid)
    await ctx.reply(r.text)


@cmd("кик актив", "Кик активных (при рейде)", "Чистка чата", 3, ["кик актив"], log=True)
async def h_kick_active(ctx: Ctx):
    secs, _ = take_period(ctx.tokens)
    secs = min(secs or 60, 600) if secs != FOREVER else 600
    r = await ctx.core.kick_many(ctx.chat_id, ctx.core.inactive_ids(ctx.chat_id, active_secs=secs), ctx.uid)
    await ctx.reply(r.text)


@cmd("кик новичков", "Кик новичков (при рейде)", "Чистка чата", 3, ["кик новичков"], log=True)
async def h_kick_new(ctx: Ctx):
    secs, _ = take_period(ctx.tokens)
    if secs is None or secs == FOREVER:
        return await ctx.err("Формат: кик новичков 1 час")
    r = await ctx.core.kick_many(ctx.chat_id, ctx.core.inactive_ids(ctx.chat_id, newcomers_secs=secs), ctx.uid)
    await ctx.reply(r.text)


def _deleted_ids(core: Core, chat_id: int):
    rows = core.db.query(
        "SELECT m.user_id FROM members m JOIN users u ON u.user_id=m.user_id WHERE m.chat_id=? AND m.left=0"
        " AND (u.first_name='Deleted Account' OR u.first_name IS NULL AND u.username IS NULL)", (chat_id,))
    return [r["user_id"] for r in rows]


@cmd("кик удалённых", "Кик удалённых аккаунтов", "Чистка чата", 3, ["кик удаленных", "кик собак"], log=True)
async def h_kick_deleted(ctx: Ctx):
    r = await ctx.core.kick_many(ctx.chat_id, _deleted_ids(ctx.core, ctx.chat_id), ctx.uid)
    await ctx.reply(r.text)


@cmd("кто удалён", "Список удалённых аккаунтов", "Чистка чата", 2, ["кто удален", "кто собака"])
async def h_who_deleted(ctx: Ctx):
    ids = _deleted_ids(ctx.core, ctx.chat_id)
    await ctx.reply(("🐶 Удалённые аккаунты:\n" + "\n".join(ctx.core.m(i) for i in ids)) if ids
                    else "Удалённых аккаунтов в чате нет.")


@cmd("кик молчунов", "Кик молчунов", "Чистка чата", 3, ["кик молчунов"], log=True)
async def h_kick_silent(ctx: Ctx):
    secs, _ = take_period(ctx.tokens)
    days = int(ctx.tokens[0]) if ctx.tokens and ctx.tokens[0].isdigit() and secs is None else None
    period = days * 86400 if days else (secs if secs and secs != FOREVER else None)
    ids = ctx.core.inactive_ids(ctx.chat_id, only_silent=True, period=period)
    r = await ctx.core.kick_many(ctx.chat_id, ids, ctx.uid)
    await ctx.reply(r.text)


@cmd("кик по смс", "Кик по числу сообщений", "Чистка чата", 3, ["кик по смс", "кик по сообщениям"], log=True)
async def h_kick_by_msgs(ctx: Ctx):
    if not ctx.tokens or not ctx.tokens[0].isdigit():
        return await ctx.err("Формат: кик по смс {число} {период}")
    secs, _ = take_period(ctx.tokens[1:])
    ids = ctx.core.inactive_ids(ctx.chat_id, min_msgs=int(ctx.tokens[0]), period=secs if secs and secs > 0 else 604800)
    r = await ctx.core.kick_many(ctx.chat_id, ids, ctx.uid)
    await ctx.reply(r.text)


@cmd("список неактив", "Список неактивных", "Чистка чата", 2, ["список неактив", "кто неактив"])
async def h_list_inactive(ctx: Ctx):
    secs, _ = take_period(ctx.tokens)
    ids = ctx.core.inactive_ids(ctx.chat_id, secs=secs if secs and secs > 0 else 30 * 86400)
    await ctx.reply(f"😴 Неактивных: {len(ids)}\n" + "\n".join(ctx.core.m(i) for i in ids[:60]))


@cmd("список молчунов", "Список молчунов", "Чистка чата", 2, ["список молчунов", "кто молчун"])
async def h_list_silent(ctx: Ctx):
    secs, _ = take_period(ctx.tokens)
    ids = ctx.core.inactive_ids(ctx.chat_id, only_silent=True, period=secs if secs and secs > 0 else None)
    await ctx.reply(f"🤐 Молчунов: {len(ids)}\n" + "\n".join(ctx.core.m(i) for i in ids[:60]))


# ============================================================ настройка чата

@cmd("закреп", "Закрепить сообщение", "Настройка чата", 2, ["закреп", "пин", "pin"], log=True)
async def h_pin(ctx: Ctx):
    r = ctx.real_reply()
    mid = r.message_id if r else (int(ctx.tokens[0]) if ctx.tokens and ctx.tokens[0].isdigit() else None)
    if not mid:
        return await ctx.err("Ответьте на сообщение или укажите его ID.")
    ok, e = await ctx.core._tg(ctx.bot.pin_chat_message(ctx.chat_id, mid, disable_notification=True))
    await ctx.reply("📌 Закреплено." if ok else f"❌ {esc(e)}")


@cmd("открепить", "Открепить сообщение", "Настройка чата", 2, ["открепить", "анпин", "unpin"], log=True)
async def h_unpin(ctx: Ctx):
    r = ctx.real_reply()
    mid = r.message_id if r else (int(ctx.tokens[0]) if ctx.tokens and ctx.tokens[0].isdigit() else None)
    ok, e = await ctx.core._tg(ctx.bot.unpin_chat_message(ctx.chat_id, mid) if mid
                               else ctx.bot.unpin_chat_message(ctx.chat_id))
    await ctx.reply("📌 Откреплено." if ok else f"❌ {esc(e)}")


@cmd("название", "Смена названия чата", "Настройка чата", 3, ["название"], log=True)
async def h_title(ctx: Ctx):
    t = " ".join(ctx.tokens).strip()
    if not t:
        return await ctx.err("Формат: !название {новое название}")
    ok, e = await ctx.core._tg(ctx.bot.set_chat_title(ctx.chat_id, t[:128]))
    await ctx.reply("✅ Название изменено." if ok else f"❌ {esc(e)}")


@cmd("описание чата", "Описание чата", "Настройка чата", 3, ["+описание чата", "-описание чата"], log=True)
async def h_chat_desc(ctx: Ctx):
    text = "" if ctx.phrase.startswith("-") else ctx.text_arg()[:255]
    ok, e = await ctx.core._tg(ctx.bot.set_chat_description(ctx.chat_id, text))
    await ctx.reply("✅ Описание обновлено." if ok else f"❌ {esc(e)}")


@cmd("чатссылка установка", "Установка чат-ссылки", "Настройка чата", 3,
     ["+чат ссылка", "+чат ссылка по заявкам", "-чат ссылка", "сброс ссылок"], log=True)
async def h_link_set(ctx: Ctx):
    old = ctx.core.get_raw(ctx.chat_id, "invite_link")
    if ctx.phrase in ("-чат ссылка", "сброс ссылок"):
        if old:
            await ctx.core._tg(ctx.bot.revoke_chat_invite_link(ctx.chat_id, old))
        ctx.core.db.execute("DELETE FROM settings WHERE chat_id=? AND key='invite_link'", (ctx.chat_id,))
        return await ctx.reply("✅ Ссылка удалена.")
    try:
        lnk = await ctx.bot.create_chat_invite_link(ctx.chat_id, name="CRYMODER",
                                                    creates_join_request=ctx.phrase.endswith("по заявкам"))
    except Exception as e:
        return await ctx.err(esc(getattr(e, "message", str(e))))
    ctx.core.db.execute("INSERT INTO settings(chat_id,key,value) VALUES(?,?,?)"
                        " ON CONFLICT(chat_id,key) DO UPDATE SET value=excluded.value",
                        (ctx.chat_id, "invite_link", lnk.invite_link))
    await ctx.reply(f"✅ Ссылка установлена: {lnk.invite_link}")


@cmd("чатссылка вызов", "Вызов чат-ссылки", "Настройка чата", 0, ["чат-ссылка", "чат ссылка"])
async def h_link_get(ctx: Ctx):
    lnk = ctx.core.get_raw(ctx.chat_id, "invite_link")
    await ctx.reply(f"🔗 {lnk}" if lnk else "Ссылка не установлена. Модератор: «+чат ссылка».")


def _text_setting(code, title, key, phrases_set, phrases_get, title_get, rank_get=0):
    @cmd(f"{code} установка", f"Установка: {title}", "Настройка чата", 3, phrases_set, log=True)
    async def _set(ctx: Ctx):
        text = ctx.text_arg()
        if not text:
            return await ctx.err("Напишите текст на следующей строке.")
        ctx.core.put(ctx.chat_id, key, text[:4000])
        await ctx.reply(f"✅ {title.capitalize()} установлено.")

    @cmd(f"{code} вызов", f"Вызов: {title}", "Настройка чата", rank_get, phrases_get)
    async def _get(ctx: Ctx):
        t = ctx.core.get(ctx.chat_id, key)
        await ctx.reply(esc(t) if t else f"{title.capitalize()} не установлено.")


_text_setting("правила", "правила", "rules", ["+правила"], ["правила"], "правила")
_text_setting("приветствие", "приветствие", "greeting", ["+приветствие"], ["приветствие"], "приветствие", 1)


def render_greeting(template: str, name_html: str, gender: str = "м", plural: bool = False) -> str:
    def repl(m):
        parts = m.group(1).split("|")
        if len(parts) == 3:
            return parts[2] if plural else (parts[0] if gender == "ж" else parts[1])
        return m.group(0)
    out = re.sub(r"\{([^{}|]+\|[^{}|]+\|[^{}|]+)\}", repl, esc(template))
    return out.replace("{имя}", name_html)


@cmd("автокик", "Автокик", "Настройка чата", 3,
     ["+автокик", "-автокик", "автокик", "+автокик молчунов", "-автокик молчунов"], log=True)
async def h_autokick(ctx: Ctx):
    c, ph = ctx.core, ctx.phrase
    if ph == "-автокик":
        c.put(ctx.chat_id, "autokick_n", 0)
        return await ctx.reply("✅ Автокик на выход отключён.")
    if ph == "-автокик молчунов":
        c.put(ctx.chat_id, "autokick_silent_secs", 0)
        return await ctx.reply("✅ Автокик молчунов отключён.")
    if ph == "+автокик молчунов":
        secs, _ = take_period(ctx.tokens)
        if not secs or secs <= 0:
            return await ctx.err("Формат: +автокик молчунов 3 дня")
        c.put(ctx.chat_id, "autokick_silent_secs", secs)
        return await ctx.reply(f"✅ Молчуны будут исключены через {fmt_duration(secs)} без сообщений.")
    toks = ctx.tokens
    n = int(toks[0]) if toks and toks[0].isdigit() else 1
    window = int(toks[1]) if len(toks) > 1 and toks[1].isdigit() else 60
    action = "ban" if toks and toks[-1].lower() in ("бан", "ban") else "kick"
    c.put(ctx.chat_id, "autokick_n", n)
    c.put(ctx.chat_id, "autokick_window", window)
    c.put(ctx.chat_id, "autokick_action", action)
    await ctx.reply(f"✅ Автокик: {n} выход(ов) за {window} мин. → {'бан' if action == 'ban' else 'кик'}.")


@cmd("чат вкл выкл", "Открыть / закрыть чат", "Настройка чата", 3, ["+чат", "-чат"], log=True)
async def h_chat_toggle(ctx: Ctx):
    r = await ctx.core.set_chat_open(ctx.chat_id, ctx.phrase == "+чат", ctx.uid)
    await ctx.reply(r.text)


_FLAGS = {"чаты": "allow_chats", "сайты": "allow_sites", "теги": "allow_tags", "каналы": "allow_channels",
          "боты": "allow_bots"}


@cmd("безопасность", "Ограничение ссылок, каналов и ботов", "Настройка чата", 3,
     [f"{s}{k}" for k in ("чаты", "сайты", "теги", "каналы", "боты", "ссылки") for s in ("+", "-")], log=True)
async def h_flags(ctx: Ctx):
    on = ctx.phrase.startswith("+")
    key = ctx.phrase[1:]
    keys = ["allow_chats", "allow_sites", "allow_tags"] if key == "ссылки" else [_FLAGS[key]]
    for k in keys:
        ctx.core.put(ctx.chat_id, k, 1 if on else 0)
    what = {"чаты": "ссылки на частные чаты", "сайты": "ссылки на сайты", "теги": "теги и ссылки Telegram",
            "каналы": "сообщения от имени каналов", "боты": "приглашение ботов",
            "ссылки": "все виды ссылок"}[key]
    await ctx.reply(f"✅ Теперь {what}: {'разрешены' if on else 'запрещены'}.")


@cmd("разрешить ссылку", "Белый список ссылок", "Настройка чата", 3, ["разрешить ссылку", "удалить ссылку"], log=True)
async def h_allow_link(ctx: Ctx):
    if not ctx.tokens:
        return await ctx.err("Укажите ссылку.")
    link = ctx.tokens[0].lower().rstrip("/")
    if ctx.phrase == "разрешить ссылку":
        ctx.core.db.execute("INSERT OR IGNORE INTO allowed_links(chat_id,link) VALUES(?,?)", (ctx.chat_id, link))
        await ctx.reply("✅ Ссылка добавлена в разрешённые.")
    else:
        ctx.core.db.execute("DELETE FROM allowed_links WHERE chat_id=? AND link=?", (ctx.chat_id, link))
        await ctx.reply("✅ Ссылка убрана из разрешённых.")


@cmd("входы выходы", "Уведомления о входах и выходах", "Настройка чата", 3,
     ["+входы", "-входы", "+выходы", "-выходы", "+входы-выходы", "-входы-выходы"], log=True)
async def h_joins(ctx: Ctx):
    on = 1 if ctx.phrase.startswith("+") else 0
    what = ctx.phrase[1:]
    if what in ("входы", "входы-выходы"):
        ctx.core.put(ctx.chat_id, "show_joins", on)
    if what in ("выходы", "входы-выходы"):
        ctx.core.put(ctx.chat_id, "show_leaves", on)
    await ctx.reply(f"✅ Уведомления «{what}»: {'включены' if on else 'выключены'}.")


@cmd("автозаявки", "Автопринятие заявок", "Настройка чата", 3, ["+автозаявки", "-автозаявки"], log=True)
async def h_autoreq(ctx: Ctx):
    on = 1 if ctx.phrase.startswith("+") else 0
    ctx.core.put(ctx.chat_id, "auto_requests", on)
    await ctx.reply(f"✅ Автопринятие заявок {'включено' if on else 'выключено'}.")


@cmd("инвайты", "Лимит приглашений", "Настройка чата", 3, ["инвайты"], log=True)
async def h_invites(ctx: Ctx):
    if not ctx.tokens or not ctx.tokens[0].isdigit():
        return await ctx.err("Формат: инвайты {число}  (0 — без лимита)")
    ctx.core.put(ctx.chat_id, "invites_limit", int(ctx.tokens[0]))
    await ctx.reply("✅ Лимит приглашений: " + (ctx.tokens[0] if ctx.tokens[0] != "0" else "снят"))


@cmd("модер теги", "Теги модераторов в ответах", "Настройка чата", 3, ["+модер теги", "-модер теги"])
async def h_modtags(ctx: Ctx):
    ctx.core.put(ctx.chat_id, "mod_tags", 1 if ctx.phrase.startswith("+") else 0)
    await ctx.reply("✅ Готово.")


@cmd("код чата", "Код чата", "Настройка чата", 1, ["код чата", "код беседы", "чат ид"])
async def h_chat_code(ctx: Ctx):
    await ctx.reply(f"🔑 Код чата: <code>{ctx.core.chat_code(ctx.chat_id)}</code>\nTelegram ID: <code>{ctx.chat_id}</code>")


@cmd("смс ид", "ID сообщения", "Настройка чата", 0, ["смс ид"])
async def h_msg_id(ctx: Ctx):
    r = ctx.real_reply()
    await ctx.reply(f"ID сообщения: <code>{r.message_id if r else ctx.msg.message_id}</code>")


@cmd("ид", "ID пользователя и чата", "Профиль", 0, ["ид", "id"], pm=True)
async def h_id(ctx: Ctx):
    lines = [f"Ваш ID: <code>{ctx.uid}</code>"]
    if ctx.msg.chat.type != "private":
        lines.append(f"ID чата: <code>{ctx.chat_id}</code>")
    r = ctx.real_reply()
    if r and r.from_user:
        lines.append(f"ID автора сообщения, на которое вы ответили: <code>{r.from_user.id}</code>")
    else:
        ids, _ = await ctx.targets()
        if ids and ids[0] != ctx.uid:
            lines.append(f"ID цели: <code>{ids[0]}</code>")
    await ctx.reply("\n".join(lines))


@cmd("обновить чат", "Обновить информацию о чате", "Настройка чата", 1, ["обновить чат"])
async def h_refresh(ctx: Ctx):
    await ctx.core.sync_admins(ctx.chat_id)
    try:
        ch = await ctx.bot.get_chat(ctx.chat_id)
        ctx.core.ensure_chat(ctx.chat_id, ch.title or "")
    except Exception:
        pass
    await ctx.reply("✅ Информация о чате обновлена.")


@cmd("тг админ", "Telegram-администраторы", "Настройка чата", 4, ["+тг админ", "-тг админ"], log=True)
async def h_tg_admin(ctx: Ctx):
    ids, rest = await ctx.targets()
    if not await _need_targets(ctx, ids):
        return
    t = ids[0]
    if ctx.phrase == "-тг админ":
        ok, e = await ctx.core._tg(ctx.bot.promote_chat_member(
            ctx.chat_id, t, can_manage_chat=False, can_delete_messages=False, can_restrict_members=False,
            can_invite_users=False, can_pin_messages=False, can_change_info=False, can_promote_members=False))
        return await ctx.reply("✅ Статус администратора снят." if ok else f"❌ {esc(e)}")
    ok, e = await ctx.core._tg(ctx.bot.promote_chat_member(
        ctx.chat_id, t, can_manage_chat=True, can_delete_messages=True, can_restrict_members=True,
        can_invite_users=True, can_pin_messages=True))
    title = " ".join(rest)[:16]
    if ok and title:
        await ctx.core._tg(ctx.bot.set_chat_administrator_custom_title(ctx.chat_id, t, title))
    await ctx.reply(f"✅ {ctx.core.m(t)} назначен администратором." if ok else f"❌ {esc(e)}")


@cmd("панель", "Вход в веб-панель", "Настройка чата", 1, ["панель", "веб", "panel", "веб-панель"], pm=True)
async def h_panel(ctx: Ctx):
    if not ctx.core.config or not ctx.core.config.web_enabled:
        return await ctx.reply("Веб-панель на этом боте отключена.")
    token = secrets.token_urlsafe(24)
    ctx.core.db.execute("INSERT INTO web_tokens(token,user_id,expires_at) VALUES(?,?,?)",
                        (token, ctx.uid, now() + 600))
    url = f"{ctx.core.config.public_url}/login?t={token}"
    try:
        await ctx.bot.send_message(ctx.uid, f"🔐 Ссылка для входа в веб-панель (действует 10 минут, одноразовая):\n{url}")
    except Exception:
        return await ctx.err("Не могу написать вам в личные сообщения. Откройте диалог с ботом и нажмите Start.")
    if ctx.msg.chat.type != "private":
        await ctx.reply("🔐 Ссылку для входа отправил вам в личные сообщения.")


# ============================================================ упоминания

@cmd("общий сбор", "Общий сбор", "Статистика", 2, ["созвать всех", "общий сбор", "созвать онлайн"], log=True)
async def h_call_all(ctx: Ctx):
    rows = ctx.core.db.query("SELECT user_id FROM members m JOIN users u USING(user_id) "
                             "WHERE m.chat_id=? AND m.left=0 AND u.is_bot=0 ORDER BY last_seen DESC LIMIT 1000",
                             (ctx.chat_id,))
    text = ctx.text_arg()
    await _mention_chunks(ctx, [r["user_id"] for r in rows], "📣 " + esc(text) if text else "📣 Общий сбор!")


@cmd("позвать молчунов", "Позвать молчунов", "Статистика", 2, ["позвать молчунов", "созвать молчунов"])
async def h_call_silent(ctx: Ctx):
    await _mention_chunks(ctx, ctx.core.inactive_ids(ctx.chat_id, only_silent=True)[:200], "📣 Молчуны, просыпайтесь!")


# ============================================================ статистика

@cmd("чат инфо", "Информация о чате", "Статистика", 0, ["чат инфо"])
async def h_chat_info(ctx: Ctx):
    o = ctx.core.chat_overview(ctx.chat_id)
    await ctx.reply(
        f"💬 <b>{esc(o['title'])}</b>\nКод: <code>{o['code']}</code>\nУчастников в базе: {o['members']}\n"
        f"Сообщений сегодня: {o['msgs_today']}, за 7 дней: {o['msgs_week']}\n"
        f"Модераторов: {o['staff']}, банов: {o['bans']}, мутов: {o['mutes']}, предупреждений: {o['warns']}")


_STAT_PERIODS = {"сутки": "сутки", "неделя": "неделя", "месяц": "месяц", "вся": "вся", "всё": "вся", "день": "сутки"}


@cmd("стата", "Статистика сообщений", "Статистика", 0,
     ["стата", "топ"] + [f"{a} {b}" for a in ("стата", "топ") for b in ("сутки", "неделя", "месяц", "вся")])
async def h_top(ctx: Ctx):
    period, limit = "сутки", 10
    words = ctx.phrase.split()[1:] + [t.lower() for t in ctx.tokens]
    for w in words:
        if w.isdigit():
            limit = max(1, min(int(w), 100))
        elif w in _STAT_PERIODS:
            period = _STAT_PERIODS[w]
    rows = ctx.core.top(ctx.chat_id, period, limit)
    if not rows:
        return await ctx.reply("Статистики пока нет.")
    medals = ["🥇", "🥈", "🥉"]
    out = [f"📊 <b>Топ за {'всё время' if period == 'вся' else period}</b>"]
    for i, r in enumerate(rows):
        out.append(f"{medals[i] if i < 3 else str(i + 1) + '.'} {ctx.core.m(r['user_id'])} — {r['cnt']}")
    await ctx.reply("\n".join(out))


@cmd("моя стата", "Моя статистика", "Статистика", 0, ["моя стата"])
async def h_my_stat(ctx: Ctx):
    c, u = ctx.core, ctx.uid
    total = c.db.scalar("SELECT msg_count FROM members WHERE chat_id=? AND user_id=?", (ctx.chat_id, u), 0)
    place = c.db.scalar("SELECT COUNT(*)+1 FROM members WHERE chat_id=? AND left=0 AND msg_count>?",
                        (ctx.chat_id, total), 1)
    await ctx.reply(f"📈 {c.m(u)}\nСегодня: {c.msgs_in(ctx.chat_id, u, 0)}\nЗа неделю: {c.msgs_in(ctx.chat_id, u, 6)}\n"
                    f"За месяц: {c.msgs_in(ctx.chat_id, u, 29)}\nВсего: {total}\nМесто в чате: {place}")


@cmd("олды", "Старожилы чата", "Статистика", 0, ["олды"])
async def h_olds(ctx: Ctx):
    rows = ctx.core.db.query("SELECT user_id, first_seen FROM members WHERE chat_id=? AND left=0"
                             " ORDER BY first_seen ASC LIMIT 20", (ctx.chat_id,))
    await ctx.reply("🏛 <b>Олды</b>\n" + "\n".join(f"{i}. {ctx.core.m(r['user_id'])} — с {fmt_ts(r['first_seen'])}"
                                                   for i, r in enumerate(rows, 1)))


@cmd("новички", "Новички чата", "Статистика", 0, ["новички"])
async def h_newbies(ctx: Ctx):
    rows = ctx.core.db.query("SELECT user_id, joined_at FROM members WHERE chat_id=? AND left=0"
                             " ORDER BY joined_at DESC LIMIT 20", (ctx.chat_id,))
    await ctx.reply("🌱 <b>Новички</b>\n" + "\n".join(f"{i}. {ctx.core.m(r['user_id'])} — {fmt_ts(r['joined_at'])}"
                                                      for i, r in enumerate(rows, 1)))


@cmd("население", "Число участников", "Статистика", 0, ["население"])
async def h_population(ctx: Ctx):
    try:
        n = await ctx.bot.get_chat_member_count(ctx.chat_id)
    except Exception:
        n = ctx.core.db.scalar("SELECT COUNT(*) FROM members WHERE chat_id=? AND left=0", (ctx.chat_id,), 0)
    await ctx.reply(f"👥 Участников в чате: {n}")


@cmd("кто добавил", "Кто добавил участника", "Статистика", 0, ["кто добавил", "кто меня добавил"])
async def h_who_added(ctx: Ctx):
    ids, _ = await ctx.targets()
    t = ids[0] if ids and ctx.phrase == "кто добавил" else ctx.uid
    r = ctx.core.db.one("SELECT invited_by FROM members WHERE chat_id=? AND user_id=?", (ctx.chat_id, t))
    await ctx.reply(f"{ctx.core.m(t)} добавил(а): {ctx.core.m(r['invited_by'])}" if r and r["invited_by"]
                    else "Неизвестно: пользователь вошёл сам или до появления бота.")


# ============================================================ профиль

def _prof(core: Core, chat_id, uid):
    return core.db.one("SELECT * FROM profiles WHERE chat_id=? AND user_id=?", (chat_id, uid))


def _prof_set(core: Core, chat_id, uid, field, value):
    core.db.execute(f"INSERT INTO profiles(chat_id,user_id,{field}) VALUES(?,?,?)"
                    f" ON CONFLICT(chat_id,user_id) DO UPDATE SET {field}=excluded.{field}", (chat_id, uid, value))


@cmd("профиль", "Профиль участника", "Профиль", 0, ["профиль", "кто я", "кто ты", "анкета", "моя анкета"])
async def h_profile(ctx: Ctx):
    ids, _ = await ctx.targets()
    t = ids[0] if ids and ctx.phrase in ("профиль", "кто ты", "анкета") else ctx.uid
    c = ctx.core
    p = _prof(c, ctx.chat_id, t)
    mem = c.db.one("SELECT * FROM members WHERE chat_id=? AND user_id=?", (ctx.chat_id, t))
    rep = c.db.one("SELECT * FROM rep WHERE chat_id=? AND user_id=?", (ctx.chat_id, t))
    rank = c.rank_of(ctx.chat_id, t)
    lines = [f"👤 {c.m(t)}", f"ID: <code>{t}</code>"]
    if p and p["nick"]:
        lines.append(f"Ник: {esc(p['nick'])}")
    if p and p["title"]:
        lines.append(f"Звание: {esc(p['title'])}")
    if p and p["motto"]:
        lines.append(f"Девиз: {esc(p['motto'])}")
    if rank:
        lines.append(f"Ранг: {esc(RANK_NAMES[rank])} ({rank})")
    if mem:
        lines.append(f"В чате с {fmt_ts(mem['first_seen'])}, сообщений: {mem['msg_count']}")
    lines.append(f"Репутация: {rep['plus'] if rep else 0}, звёздность: {rep['star'] if rep else 0}")
    w = len(c.active_warns(ctx.chat_id, t))
    if w:
        lines.append(f"⚠️ Предупреждений: {w}")
    if p and p["about"]:
        lines.append(f"\n{esc(p['about'])}")
    await ctx.reply("\n".join(lines))


def _simple_field(code, field, title, plus, minus, show, maxlen):
    @cmd(code, title, "Профиль", 0, plus + minus + show)
    async def _h(ctx: Ctx):
        if ctx.phrase in minus:
            _prof_set(ctx.core, ctx.chat_id, ctx.uid, field, None)
            return await ctx.reply(f"✅ {title} удалено.")
        if ctx.phrase in plus:
            text = ctx.text_arg()
            if not text:
                return await ctx.err("Напишите текст после команды.")
            if len(text) > maxlen:
                return await ctx.err(f"Не больше {maxlen} символов.")
            _prof_set(ctx.core, ctx.chat_id, ctx.uid, field, text)
            return await ctx.reply(f"✅ {title} установлено.")
        ids, _ = await ctx.targets()
        p = _prof(ctx.core, ctx.chat_id, ids[0] if ids else ctx.uid)
        v = p[field] if p else None
        await ctx.reply(esc(v) if v else f"{title} не установлено.")


_simple_field("ник", "nick", "Ник", ["+ник"], ["-ник", "ник удалить"], ["ник"], 30)
_simple_field("звание", "title", "Звание", ["+звание"], ["-звание", "звание удалить"], ["звание"], 30)
_simple_field("девиз", "motto", "Девиз", ["+девиз"], ["-девиз"], ["девиз"], 100)
_simple_field("о себе", "about", "Описание", ["о себе"], ["-о себе"], ["описание"], 3800)


@cmd("мой пол", "Мой пол", "Профиль", 0, ["мой пол", "-мой пол"])
async def h_gender(ctx: Ctx):
    g = (ctx.tokens[0].lower() if ctx.tokens else "")
    if ctx.phrase == "-мой пол":
        _prof_set(ctx.core, ctx.chat_id, ctx.uid, "gender", None)
        return await ctx.reply("✅ Пол удалён.")
    if g not in ("м", "ж", "др"):
        return await ctx.err("Формат: мой пол м | ж | др")
    _prof_set(ctx.core, ctx.chat_id, ctx.uid, "gender", g)
    await ctx.reply("✅ Пол сохранён.")


# ============================================================ репутация

PLUS_LIMIT = [1, 3, 5, 10, 25, 50]
REP_WORDS = {"плюс", "лайк", "респект", "f", "уважение", "увожение", "жиза", "жыза", "красавчик", "справедливо"}


async def reputation(ctx: Ctx, sign: int, amount: int, star: bool = False) -> bool:
    r = ctx.real_reply()
    if not r or not r.from_user or r.from_user.id == ctx.uid:
        return False
    t = r.from_user.id
    if t == ctx.core.me_id:
        return False
    code = "плюсы" if sign > 0 and not star else ("звёзды" if star else "минусы")
    if not ctx.core.has_access(ctx.chat_id, ctx.uid, code):
        return False
    ctx.core.reg_user(r.from_user)
    amount = min(amount, PLUS_LIMIT[ctx.rank])
    col = "star" if star else "plus"
    ctx.core.db.execute(
        f"INSERT INTO rep(chat_id,user_id,{col}) VALUES(?,?,?) ON CONFLICT(chat_id,user_id)"
        f" DO UPDATE SET {col}={col}+excluded.{col}", (ctx.chat_id, t, sign * amount))
    row = ctx.core.db.one("SELECT * FROM rep WHERE chat_id=? AND user_id=?", (ctx.chat_id, t))
    name = ctx.core.m(t)
    if star:
        await ctx.reply(f"⭐️ Звёздность {name}: {row['star']}")
    else:
        await ctx.reply(f"{'👍' if sign > 0 else '👎'} Репутация {name}: {row['plus']} "
                        f"({'+' if sign > 0 else '-'}{amount})")
    return True


@cmd("плюсы", "Выдача плюсов", "Репутация", 0, [])
async def _plus_stub(ctx: Ctx):
    pass


@cmd("минусы", "Выдача минусов", "Репутация", 1, [])
async def _minus_stub(ctx: Ctx):
    pass


@cmd("звёзды", "Выдача звёзд", "Репутация", 0, [])
async def _star_stub(ctx: Ctx):
    pass


@cmd("рейтинг", "Рейтинг репутации", "Репутация", 0, ["рейтинг"])
async def h_rating(ctx: Ctx):
    rows = ctx.core.db.query("SELECT user_id, plus FROM rep WHERE chat_id=? AND plus!=0 ORDER BY plus DESC LIMIT 20",
                             (ctx.chat_id,))
    if not rows:
        return await ctx.reply("Рейтинг пока пуст. Ставьте «+» в ответ на сообщения!")
    await ctx.reply("🏆 <b>Рейтинг</b>\n" + "\n".join(f"{i}. {ctx.core.m(r['user_id'])} — {r['plus']}"
                                                      for i, r in enumerate(rows, 1)))


@cmd("сброс рейтинга", "Сброс рейтинга", "Репутация", 4, ["сбросить рейтинг", "обнулить рейтинг"], log=True)
async def h_rating_reset(ctx: Ctx):
    ctx.core.db.execute("UPDATE rep SET plus=0 WHERE chat_id=?", (ctx.chat_id,))
    await ctx.reply("✅ Рейтинг обнулён.")


@cmd("звёздность", "Звёздность", "Репутация", 0, ["звездность", "моя звездность", "звезды чата"])
async def h_stars(ctx: Ctx):
    if ctx.phrase == "звезды чата":
        rows = ctx.core.db.query("SELECT user_id, star FROM rep WHERE chat_id=? AND star!=0 ORDER BY star DESC LIMIT 20",
                                 (ctx.chat_id,))
        return await ctx.reply("✨ <b>Звёзды чата</b>\n" + ("\n".join(
            f"{i}. {ctx.core.m(r['user_id'])} — {r['star']}" for i, r in enumerate(rows, 1)) or "пока пусто"))
    ids, _ = await ctx.targets()
    t = ids[0] if ids and ctx.phrase == "звездность" else ctx.uid
    s = ctx.core.db.scalar("SELECT star FROM rep WHERE chat_id=? AND user_id=?", (ctx.chat_id, t), 0)
    await ctx.reply(f"✨ Звёздность {ctx.core.m(t)}: {s}")


# ============================================================ сетка чатов

def _grid_of(core: Core, chat_id: int):
    return core.db.scalar("SELECT grid_id FROM chats WHERE chat_id=?", (chat_id,))


def _grid_chats(core: Core, grid_id: int):
    return [r["chat_id"] for r in core.db.query("SELECT chat_id FROM chats WHERE grid_id=?", (grid_id,))]


@cmd("сетка создать", "Создать сетку чатов", "Сетка чатов", 5, ["+сетка", "сетка создать"], log=True)
async def h_grid_create(ctx: Ctx):
    g = _grid_of(ctx.core, ctx.chat_id)
    if not g:
        code = secrets.token_hex(4).upper()
        g = ctx.core.db.execute("INSERT INTO grids(owner_id,code,created_at) VALUES(?,?,?)", (ctx.uid, code, now()))
        ctx.core.db.execute("UPDATE chats SET grid_id=? WHERE chat_id=?", (g, ctx.chat_id))
    code = ctx.core.db.scalar("SELECT code FROM grids WHERE grid_id=?", (g,))
    await ctx.reply(f"🕸 Сетка создана. Код сетки: <code>{code}</code>\n"
                    f"В других чатах (5 ранг) напишите: <code>сетка привязать {code}</code>")


@cmd("сетка привязать", "Привязать чат к сетке", "Сетка чатов", 5, ["сетка привязать"], log=True)
async def h_grid_join(ctx: Ctx):
    if not ctx.tokens:
        return await ctx.err("Формат: сетка привязать {код сетки}")
    g = ctx.core.db.one("SELECT * FROM grids WHERE code=?", (ctx.tokens[0].upper(),))
    if not g:
        return await ctx.err("Сетка с таким кодом не найдена.")
    if g["owner_id"] != ctx.uid:
        return await ctx.err("Привязать чат может только владелец сетки.")
    ctx.core.db.execute("UPDATE chats SET grid_id=? WHERE chat_id=?", (g["grid_id"], ctx.chat_id))
    await ctx.reply("✅ Чат привязан к сетке.")


@cmd("чаты", "Список чатов сетки", "Сетка чатов", 0, ["чаты"])
async def h_grid_list(ctx: Ctx):
    g = _grid_of(ctx.core, ctx.chat_id)
    if not g:
        return await ctx.reply("Чат не состоит в сетке.")
    rows = [ctx.core.db.one("SELECT title FROM chats WHERE chat_id=?", (c,)) for c in _grid_chats(ctx.core, g)]
    await ctx.reply("🕸 <b>Сетка чатов</b>\n" + "\n".join(f"• {esc(r['title'])}" for r in rows))


async def _grid_each(ctx: Ctx, fn):
    g = _grid_of(ctx.core, ctx.chat_id)
    if not g:
        return await ctx.err("Чат не состоит в сетке («+сетка» создаёт её).")
    owner = ctx.core.db.scalar("SELECT owner_id FROM grids WHERE grid_id=?", (g,))
    if owner != ctx.uid and ctx.rank < 5:
        return await ctx.err("Только владелец сетки.")
    ok = 0
    for c in _grid_chats(ctx.core, g):
        r = await fn(c)
        ok += 1 if r and r.ok else 0
    await ctx.reply(f"✅ Выполнено в чатах: {ok}.")


@cmd("глобан", "Бан во всей сетке", "Сетка чатов", 5, ["глобан"], log=True)
async def h_gban(ctx: Ctx):
    ids, rest = await ctx.targets()
    if not await _need_targets(ctx, ids):
        return
    g = _grid_of(ctx.core, ctx.chat_id)
    if g:
        ctx.core.db.execute("INSERT OR REPLACE INTO gbans(grid_id,user_id,by_id,reason,at) VALUES(?,?,?,?,?)",
                            (g, ids[0], ctx.uid, ctx.reason(rest), now()))
    await _grid_each(ctx, lambda c: ctx.core.ban(c, ids[0], ctx.uid, None, ctx.reason(rest)))


@cmd("глоразбан", "Разбан во всей сетке", "Сетка чатов", 5, ["глоразбан"], log=True)
async def h_gunban(ctx: Ctx):
    ids, _ = await ctx.targets()
    if not await _need_targets(ctx, ids):
        return
    g = _grid_of(ctx.core, ctx.chat_id)
    if g:
        ctx.core.db.execute("DELETE FROM gbans WHERE grid_id=? AND user_id=?", (g, ids[0]))
    await _grid_each(ctx, lambda c: ctx.core.unban(c, ids[0], ctx.uid))


@cmd("сетка кик", "Кик из всей сетки", "Сетка чатов", 5, ["сетка кик"], log=True)
async def h_grid_kick(ctx: Ctx):
    ids, _ = await ctx.targets()
    if not await _need_targets(ctx, ids):
        return
    await _grid_each(ctx, lambda c: ctx.core.kick(c, ids[0], ctx.uid))


@cmd("сетка баны", "Баны сетки", "Сетка чатов", 5, ["сетка баны"])
async def h_grid_bans(ctx: Ctx):
    g = _grid_of(ctx.core, ctx.chat_id)
    rows = ctx.core.db.query("SELECT * FROM gbans WHERE grid_id=?", (g,)) if g else []
    await ctx.reply("🚫 <b>Глобальные баны</b>\n" + ("\n".join(
        f"{ctx.core.m(r['user_id'])}" + (f" — {esc(r['reason'])}" if r["reason"] else "") for r in rows) or "пусто"))


@cmd("сетка модер", "Назначить модератора во всей сетке", "Сетка чатов", 5,
     ["сетка модер", "сетка разжаловать"], log=True)
async def h_grid_mod(ctx: Ctx):
    toks = list(ctx.tokens)
    n = int(toks[0]) if toks and re.fullmatch(r"[1-5]", toks[0]) else (ctx.bang or 1)
    if toks and re.fullmatch(r"[1-5]", toks[0]):
        toks = toks[1:]
    ids, _ = await ctx.targets(toks)
    if not await _need_targets(ctx, ids):
        return
    new = 0 if ctx.phrase == "сетка разжаловать" else n
    await _grid_each(ctx, lambda c: _as_result(ctx.core.set_rank(c, ids[0], new, ctx.uid)))


async def _as_result(r):
    return r


# ============================================================ автомодерация

_WORD_RE = re.compile(r"^[\w\-]{2,40}$")


@cmd("слово добавить", "Добавить слово в фильтр", "Автомодерация", 2, ["addword", "+слово"], log=True)
async def h_addword(ctx: Ctx):
    w = norm(ctx.tokens[0]) if ctx.tokens else ""
    if not _WORD_RE.match(w):
        return await ctx.err("Формат: +слово {слово} — одно слово, 2–40 символов.")
    ctx.core.add_word(ctx.chat_id, w)
    ctx.core.audit(ctx.chat_id, ctx.uid, "+слово", w, "chat")
    await ctx.reply(f"✅ Слово <code>{esc(w)}</code> добавлено в фильтр.")


@cmd("слово удалить", "Убрать слово из фильтра", "Автомодерация", 2, ["delword", "-слово"], log=True)
async def h_delword(ctx: Ctx):
    w = norm(ctx.tokens[0]) if ctx.tokens else ""
    if not w:
        return await ctx.err("Формат: -слово {слово}")
    if not ctx.core.del_word(ctx.chat_id, w):
        return await ctx.err("Такого слова в фильтре нет.")
    ctx.core.audit(ctx.chat_id, ctx.uid, "-слово", w, "chat")
    await ctx.reply(f"✅ Слово <code>{esc(w)}</code> убрано из фильтра.")


@cmd("слова", "Список запрещённых слов", "Автомодерация", 2, ["words", "слова"])
async def h_words(ctx: Ctx):
    ws = ctx.core.words(ctx.chat_id)
    await ctx.reply("🚫 Запрещённые слова: " + ", ".join(f"<code>{esc(w)}</code>" for w in ws)
                    if ws else "Фильтр слов пуст. Добавить: +слово {слово}")


@cmd("настройки", "Панель настроек автомодерации", "Автомодерация", 2, ["settings", "настройки"])
async def h_settings(ctx: Ctx):
    await ctx.reply(settings_ui.TITLE, markup=settings_ui.build_keyboard(ctx.core, ctx.chat_id))


# ============================================================ прочее

@cmd("пинг", "Проверка работы бота", "Профиль", 0, ["пинг", "кинг", "пиу", "бот"])
async def h_ping(ctx: Ctx):
    await ctx.reply({"пинг": "Понг", "кинг": "Конг", "пиу": "Пау", "бот": "На месте"}[ctx.phrase])


HELP = (
    "<b>CRYMODER</b> — бесплатный чат-менеджер в стиле Iris.\n\n"
    "<b>Модерация:</b> +модер [1-5] @ник, снять @ник, кто админ, модер лог\n"
    "<b>Наказания:</b> варн/мут/бан [срок] @ник (причина на следующей строке), -варн, -мут, разбан, кик, "
    "банлист, муты, варнлист, мои варны, амнистия, гб @ник (голосование)\n"
    "Те же команды со слешем: /warn /unwarn /warns /resetwarns /mute /unmute /ban /unban /kick /del "
    "(срок: 30m, 2h, 1d или «2 часа»; цель — ответом, @ником или ID)\n"
    "<b>Чистка:</b> -смс N, пург, кик неактив 2 месяца, кик новичков 1 час, кик молчунов\n"
    "<b>Автомодерация:</b> /settings (антифлуд, капча, ссылки, слова), +слово, -слово, слова, "
    "варны лимит/действие\n"
    "<b>Настройки:</b> +правила, +приветствие, +автокик, -чат/+чат, -ссылки, -сайты, -боты, +автозаявки\n"
    "<b>Доступ команд:</b> доступ команд, дк бан 2, -дк бан, мой дк, +лдк\n"
    "<b>Статистика:</b> топ, стата неделя, моя стата, чат инфо, олды, новички\n"
    "<b>Профиль:</b> профиль, +ник, +звание, +девиз, о себе, /id\n"
    "<b>Репутация:</b> «+» или «-» в ответ на сообщение, рейтинг\n"
    "<b>Веб-панель:</b> «панель» — пришлю ссылку для входа.\n"
    "Префиксы: «!», «.», «/», «Ирис». Команды модерации работают и без префикса. "
    "Админы Telegram с правом блокировки автоматически получают 2 ранг.")


@cmd("помощь", "Помощь", "Профиль", 0, ["помощь", "help", "команды", "start"], pm=True)
async def h_help(ctx: Ctx):
    await ctx.reply(HELP)


# ============================================================ диспетчер

registry.rebuild_index()

_PREFIX = re.compile(r"^(?:[!./]|(?:ирис|ириска)[,:]?\s+)", re.I)
_BANG = re.compile(r"^(!{2,5})(модер|админ)(?=\s|$)", re.I)
_REP_RE = re.compile(r"^(\++|-+|\+\d+|-\d+|\*\d*|\^\d*)$")


def unprefixed_ok(c, phrase: str, has_args: bool) -> bool:
    """Без префикса: модераторские команды, «+/-»-команды, точные фразы без аргументов («кто админ», «топ»)
    и статистика. Обычные слова с аргументами («ник Вася») требуют префикса, чтобы не ловить болтовню."""
    if phrase[:1] in "+-":
        return True
    if phrase in AMBIGUOUS:
        return False
    if c.code == "стата":
        return True
    return c.default_rank >= 1 or not has_args


async def handle_message(core: Core, bot, msg) -> bool:
    """Возвращает True, если сообщение было командой."""
    text = (msg.text or msg.caption or "").strip()
    if not text or not msg.from_user:
        return False
    lines = text.split("\n")
    mention_m = re.match(r"^/\S+?@(\w+)", lines[0].strip())
    if mention_m and core.me_username and mention_m.group(1).lower() != core.me_username.lower():
        return False                       # команда адресована другому боту
    first = re.sub(r"^(/\S+?)@\w+", r"\1", lines[0].strip())
    in_group = msg.chat.type in ("group", "supergroup")

    # репутация: «+», «++», «-3», «*» в ответ на сообщение
    if in_group and msg.reply_to_message and (_REP_RE.match(first) or first.lower() in REP_WORDS):
        ctx = Ctx(core, bot, msg, [], lines, True, 0, "")
        if first.lower() in REP_WORDS:
            return await reputation(ctx, 1, 1)
        sign = -1 if first[0] == "-" else 1
        star = first[0] in "*^"
        digits = re.sub(r"\D", "", first)
        amount = int(digits) if digits else (len(first) if first[0] in "+-" else 1)
        return await reputation(ctx, sign, amount, star)

    prefixed, bang = False, 0
    mb = _BANG.match(first)
    if mb:
        bang, prefixed = len(mb.group(1)), True
        first = mb.group(2) + first[mb.end():]
    else:
        mp = _PREFIX.match(first)
        if mp:
            prefixed = True
            first = first[mp.end():].strip()
    line_norm = norm(first)
    c, n = find_command(line_norm)
    if not c:
        return False
    phrase = " ".join(line_norm.split()[:n])
    if not prefixed and not unprefixed_ok(c, phrase, len(first.split()) > n):
        return False
    toks = first.split()[n:]
    ctx = Ctx(core, bot, msg, toks, lines, prefixed, bang, phrase)

    if in_group:
        core.ensure_chat(ctx.chat_id, msg.chat.title or "")
        if not core.creator_of(ctx.chat_id):
            await core.maybe_sync_admins(ctx.chat_id)
        if not core.has_access(ctx.chat_id, ctx.uid, c.code):
            if prefixed:
                await ctx.reply("⛔ Недостаточно прав для этой команды.")
            return True
    elif msg.chat.type == "private":
        if not c.pm:
            if prefixed:
                await ctx.reply("Эта команда работает в чатах. В личке доступны «панель» и «помощь».")
            return True
        if c.code == "панель" and not _can_open_panel(core, ctx.uid):
            return await _deny_panel(ctx)
    else:
        return False

    try:
        await c.handler(ctx)
    except Exception:
        log.exception("command %s failed", c.code)
        try:
            await ctx.reply("⚠️ Внутренняя ошибка при выполнении команды.")
        except Exception:
            pass
    return True


def _can_open_panel(core: Core, uid: int) -> bool:
    if uid in core.super_admins:
        return True
    return bool(core.db.one("SELECT 1 FROM ranks WHERE user_id=? AND rank>=1", (uid,)))


async def _deny_panel(ctx: Ctx):
    await ctx.reply("Веб-панель доступна модераторам чатов, где работает этот бот.")
    return True
