"""Ядро: все действия модерации. Используется и командами бота, и веб-панелью,
поэтому права проверяются здесь, а не в обработчиках."""
import json
import random
import string
from dataclasses import dataclass
from datetime import datetime, timezone

from aiogram.types import ChatPermissions

from . import registry
from .util import FOREVER, esc, fmt_duration, fmt_until, full_name, mention, now, today_str, day_ago

RANK_NAMES = {0: "участник", 1: "младший модератор", 2: "старший модератор",
              3: "младший администратор", 4: "старший администратор", 5: "создатель"}
DISABLED = 6

# ключ: (тип, по умолчанию, описание)
SETTINGS = {
    "warn_limit": ("int", 3, "Лимит предупреждений до исключения"),
    "warn_ban_secs": ("secs", 0, "Срок бана при лимите варнов (0 — просто кик, -1 — навсегда)"),
    "warn_ttl_secs": ("secs", 0, "Срок хранения предупреждения (0 — бессрочно)"),
    "mute_default_secs": ("secs", 604800, "Срок мута по умолчанию"),
    "ban_default_secs": ("secs", 0, "Срок бана по умолчанию (0 — навсегда)"),
    "greeting": ("text", "", "Приветствие ({имя}, {ж|м|мн})"),
    "rules": ("text", "", "Правила чата"),
    "allow_chats": ("bool", 1, "Разрешить ссылки на частные чаты"),
    "allow_sites": ("bool", 1, "Разрешить ссылки на сторонние сайты"),
    "allow_tags": ("bool", 1, "Разрешить @теги и ссылки на ресурсы Telegram"),
    "allow_channels": ("bool", 1, "Разрешить писать от имени каналов"),
    "allow_bots": ("bool", 1, "Разрешить участникам приглашать ботов"),
    "invites_limit": ("int", 0, "Лимит приглашений за 10 минут (0 — без лимита)"),
    "auto_requests": ("bool", 0, "Автоматически принимать заявки на вступление"),
    "show_joins": ("bool", 0, "Уведомлять о входах"),
    "show_leaves": ("bool", 0, "Уведомлять о выходах"),
    "autokick_n": ("int", 0, "Автокик: число выходов (0 — выключен)"),
    "autokick_window": ("int", 60, "Автокик: окно, минут"),
    "autokick_action": ("choice:kick,ban", "kick", "Автокик: наказание"),
    "autokick_silent_secs": ("secs", 0, "Автокик молчунов: срок без сообщений (0 — выключен)"),
    "mod_tags": ("bool", 0, "Показывать ID модератора в ответах"),
}


@dataclass
class Result:
    ok: bool
    text: str

    def __iter__(self):
        yield self.ok
        yield self.text


def OK(t):
    return Result(True, t)


def ERR(t):
    return Result(False, t)


class Core:
    def __init__(self, db, bot=None):
        self.db = db
        self.bot = bot
        self.me_id = 0
        self.me_username = ""

    # ---------- Telegram helper ----------
    async def _tg(self, coro):
        try:
            await coro
            return True, ""
        except Exception as e:  # TelegramAPIError и прочее
            msg = getattr(e, "message", None) or str(e)
            return False, msg

    # ---------- пользователи ----------
    def reg_user(self, u) -> None:
        if u is None:
            return
        self.db.execute(
            "INSERT INTO users(user_id,username,first_name,last_name,is_bot,first_seen) VALUES(?,?,?,?,?,?)"
            " ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,"
            " first_name=excluded.first_name,last_name=excluded.last_name",
            (u.id, (u.username or "").lower() or None, u.first_name, u.last_name, int(bool(u.is_bot)), now()))

    def name(self, uid: int) -> str:
        r = self.db.one("SELECT * FROM users WHERE user_id=?", (uid,))
        if not r:
            return str(uid)
        return full_name(r["first_name"], r["last_name"], r["username"], uid)

    def m(self, uid: int) -> str:
        return mention(uid, self.name(uid))

    def user_by_username(self, uname: str):
        r = self.db.one("SELECT user_id FROM users WHERE username=?", (uname.lower().lstrip("@"),))
        return r["user_id"] if r else None

    # ---------- чаты ----------
    def ensure_chat(self, chat_id: int, title: str = "") -> None:
        r = self.db.one("SELECT 1 FROM chats WHERE chat_id=?", (chat_id,))
        if r:
            if title:
                self.db.execute("UPDATE chats SET title=?, active=1 WHERE chat_id=?", (title, chat_id))
            return
        while True:
            code = "".join(random.choices(string.ascii_uppercase + string.digits, k=8))
            if not self.db.one("SELECT 1 FROM chats WHERE code=?", (code,)):
                break
        self.db.execute("INSERT INTO chats(chat_id,title,code,created_at) VALUES(?,?,?,?)",
                        (chat_id, title, code, now()))

    def chat_code(self, chat_id: int) -> str:
        return self.db.scalar("SELECT code FROM chats WHERE chat_id=?", (chat_id,), "")

    def chat_by_code(self, code: str):
        r = self.db.one("SELECT chat_id FROM chats WHERE code=?", (code.upper(),))
        return r["chat_id"] if r else None

    def creator_of(self, chat_id: int):
        return self.db.scalar("SELECT creator_id FROM chats WHERE chat_id=?", (chat_id,))

    async def sync_creator(self, chat_id: int) -> None:
        """Определяет создателя чата через Telegram и даёт ему 5 ранг."""
        try:
            admins = await self.bot.get_chat_administrators(chat_id)
        except Exception:
            return
        for a in admins:
            if getattr(a, "status", "") == "creator":
                self.reg_user(a.user)
                old = self.creator_of(chat_id)
                self.db.execute("UPDATE chats SET creator_id=? WHERE chat_id=?", (a.user.id, chat_id))
                if old != a.user.id and self.rank_of(chat_id, a.user.id) < 5:
                    self._put_rank(chat_id, a.user.id, 5, 0)

    # ---------- настройки ----------
    def get(self, chat_id: int, key: str):
        typ, default, _ = SETTINGS[key]
        r = self.db.one("SELECT value FROM settings WHERE chat_id=? AND key=?", (chat_id, key))
        if not r:
            return default
        v = r["value"]
        if typ in ("int", "secs", "bool"):
            try:
                return int(v)
            except ValueError:
                return default
        return v

    def put(self, chat_id: int, key: str, value) -> None:
        self.db.execute("INSERT INTO settings(chat_id,key,value) VALUES(?,?,?)"
                        " ON CONFLICT(chat_id,key) DO UPDATE SET value=excluded.value",
                        (chat_id, key, str(value)))

    def all_settings(self, chat_id: int) -> list[dict]:
        out = []
        for k, (typ, default, desc) in SETTINGS.items():
            out.append({"key": k, "type": typ, "value": self.get(chat_id, k), "default": default, "desc": desc})
        return out

    def set_setting(self, chat_id: int, key: str, raw, by: int, source="web") -> Result:
        if key not in SETTINGS:
            return ERR("Неизвестная настройка")
        typ = SETTINGS[key][0]
        try:
            if typ == "bool":
                raw = 1 if str(raw).lower() in ("1", "true", "on", "да") else 0
            elif typ in ("int", "secs"):
                raw = int(raw)
                if key == "warn_limit" and not 1 <= raw <= 50:
                    return ERR("Лимит варнов: от 1 до 50")
                if typ == "int" and raw < 0:
                    return ERR("Значение не может быть отрицательным")
            elif typ.startswith("choice:"):
                if str(raw) not in typ.split(":", 1)[1].split(","):
                    return ERR("Недопустимое значение")
            else:
                raw = str(raw)
                if len(raw) > 4000:
                    return ERR("Слишком длинный текст")
        except ValueError:
            return ERR("Нужно число")
        self.put(chat_id, key, raw)
        self.audit(chat_id, by, "настройка", f"{key} = {str(raw)[:80]}", source)
        return OK("Сохранено")

    # ---------- журнал ----------
    def audit(self, chat_id, by, action, detail="", source="chat") -> None:
        self.db.execute("INSERT INTO audit(chat_id,by_id,action,detail,source,at) VALUES(?,?,?,?,?,?)",
                        (chat_id, by, action, detail, source, now()))

    # ---------- ранги ----------
    def rank_of(self, chat_id: int, uid: int) -> int:
        if uid == self.me_id and uid:
            return 5
        return self.db.scalar("SELECT rank FROM ranks WHERE chat_id=? AND user_id=?", (chat_id, uid), 0)

    def _put_rank(self, chat_id, uid, new, by) -> int:
        old = self.rank_of(chat_id, uid)
        if new <= 0:
            self.db.execute("DELETE FROM ranks WHERE chat_id=? AND user_id=?", (chat_id, uid))
        else:
            self.db.execute("INSERT INTO ranks(chat_id,user_id,rank,appointed_by,at) VALUES(?,?,?,?,?)"
                            " ON CONFLICT(chat_id,user_id) DO UPDATE SET rank=excluded.rank,"
                            " appointed_by=excluded.appointed_by, at=excluded.at",
                            (chat_id, uid, new, by, now()))
        if old != new:
            self.db.execute("INSERT INTO rank_log(chat_id,user_id,old,new,by_id,at) VALUES(?,?,?,?,?,?)",
                            (chat_id, uid, old, new, by, now()))
        return old

    def set_rank(self, chat_id, target, new, by, source="chat") -> Result:
        if not 0 <= new <= 5:
            return ERR("Ранг от 0 до 5")
        if target == self.me_id:
            return ERR("Боту ранг назначать нельзя")
        br, tr = self.rank_of(chat_id, by), self.rank_of(chat_id, target)
        if by != 0:
            if br < 5 and (br <= new or br <= tr):
                return ERR("Недостаточно прав: можно назначать только ранг ниже своего "
                           "и только тем, кто ниже вас по рангу.")
            if br == 5 and tr == 5 and target != by and new < 5 and self.creator_of(chat_id) == target:
                return ERR("Создателя чата нельзя разжаловать другим создателем.")
        old = self._put_rank(chat_id, target, new, by)
        self.audit(chat_id, by, "ранг", f"{target}: {old} → {new}", source)
        if new == 0:
            return OK(f"{self.m(target)} больше не модератор.")
        return OK(f"{self.m(target)} теперь {esc(RANK_NAMES[new])} ({new} ранг).")

    def staff(self, chat_id: int):
        return self.db.query("SELECT user_id, rank, appointed_by, at FROM ranks WHERE chat_id=? AND rank>0"
                             " ORDER BY rank DESC, at", (chat_id,))

    # ---------- доступ команд ----------
    def required_rank(self, chat_id: int, code: str) -> int:
        r = self.db.one("SELECT rank FROM cmd_access WHERE chat_id=? AND code=?", (chat_id, code))
        if r:
            return r["rank"]
        return registry.COMMANDS[code].default_rank

    def has_access(self, chat_id: int, uid: int, code: str) -> bool:
        pr = self.db.one("SELECT allowed FROM cmd_access_user WHERE chat_id=? AND user_id=? AND code=?",
                         (chat_id, uid, code))
        req = self.required_rank(chat_id, code)
        if req >= DISABLED:
            return False
        if pr is not None:
            return bool(pr["allowed"])
        return self.rank_of(chat_id, uid) >= req

    def set_access(self, chat_id: int, code: str, rank: int, by: int, source="chat") -> Result:
        if code not in registry.COMMANDS:
            return ERR("Неизвестная команда")
        if not 0 <= rank <= 6:
            return ERR("Ранг от 0 до 5, 6 — отключить")
        old = self.required_rank(chat_id, code)
        default = registry.COMMANDS[code].default_rank
        if rank == default:
            self.db.execute("DELETE FROM cmd_access WHERE chat_id=? AND code=?", (chat_id, code))
        else:
            self.db.execute("INSERT INTO cmd_access(chat_id,code,rank) VALUES(?,?,?)"
                            " ON CONFLICT(chat_id,code) DO UPDATE SET rank=excluded.rank", (chat_id, code, rank))
        self.db.execute("INSERT INTO access_log(chat_id,code,old,new,by_id,at) VALUES(?,?,?,?,?,?)",
                        (chat_id, code, old, rank, by, now()))
        self.audit(chat_id, by, "дк", f"{code}: {old} → {rank}", source)
        title = registry.COMMANDS[code].title
        return OK(f"Доступ «{esc(title)}»: {'отключена' if rank >= DISABLED else 'ранг ' + str(rank)}.")

    def reset_access(self, chat_id: int, by: int, code: str = None, source="chat") -> Result:
        if code:
            self.db.execute("DELETE FROM cmd_access WHERE chat_id=? AND code=?", (chat_id, code))
        else:
            self.db.execute("DELETE FROM cmd_access WHERE chat_id=?", (chat_id,))
        self.audit(chat_id, by, "дк сброс", code or "все", source)
        return OK("Настройки доступа сброшены.")

    def set_personal(self, chat_id, uid, code, allowed: int | None, by, source="chat") -> Result:
        if code not in registry.COMMANDS:
            return ERR("Неизвестная команда")
        if registry.COMMANDS[code].section == "Доступ команд":
            return ERR("Для раздела управления доступом исключения ставить нельзя.")
        if allowed is None:
            self.db.execute("DELETE FROM cmd_access_user WHERE chat_id=? AND user_id=? AND code=?",
                            (chat_id, uid, code))
        else:
            self.db.execute("INSERT INTO cmd_access_user(chat_id,user_id,code,allowed) VALUES(?,?,?,?)"
                            " ON CONFLICT(chat_id,user_id,code) DO UPDATE SET allowed=excluded.allowed",
                            (chat_id, uid, code, allowed))
        self.audit(chat_id, by, "лдк", f"{uid}:{code}={allowed}", source)
        return OK("Личный доступ обновлён.")

    # ---------- проверки для наказаний ----------
    def _check(self, chat_id, by, target) -> Result | None:
        if target == self.me_id:
            return ERR("Бота наказывать нельзя.")
        if by and self.rank_of(chat_id, by) <= self.rank_of(chat_id, target):
            return ERR("Недостаточно прав: ранг цели не ниже вашего.")
        return None

    def _tag(self, chat_id, by) -> str:
        return f" <code>[{by}]</code>" if by and self.get(chat_id, "mod_tags") else ""

    # ---------- предупреждения ----------
    def active_warns(self, chat_id, uid):
        return self.db.query("SELECT * FROM warns WHERE chat_id=? AND user_id=?"
                             " AND (expires_at=0 OR expires_at>?) ORDER BY id", (chat_id, uid, now()))

    async def warn(self, chat_id, target, by, secs=None, reason="", source="chat") -> Result:
        bad = self._check(chat_id, by, target)
        if bad:
            return bad
        if secs is None:
            ttl = self.get(chat_id, "warn_ttl_secs")
            secs = ttl if ttl > 0 else 0
        exp = 0 if secs in (0, FOREVER) else now() + secs
        self.db.execute("INSERT INTO warns(chat_id,user_id,by_id,reason,created_at,expires_at) VALUES(?,?,?,?,?,?)",
                        (chat_id, target, by, reason, now(), exp))
        n = len(self.active_warns(chat_id, target))
        limit = self.get(chat_id, "warn_limit")
        self.audit(chat_id, by, "варн", f"{target} ({n}/{limit}) {reason}"[:200], source)
        out = f"⚠️ {self.m(target)} получает предупреждение ({n}/{limit}).{self._tag(chat_id, by)}"
        if reason:
            out += f"\nПричина: {esc(reason)}"
        if n >= limit:
            bsecs = self.get(chat_id, "warn_ban_secs")
            self.db.execute("DELETE FROM warns WHERE chat_id=? AND user_id=?", (chat_id, target))
            if bsecs == 0:
                r = await self._remove(chat_id, target, by=0, ban=False, reason="лимит предупреждений", source=source)
            else:
                r = await self.ban(chat_id, target, 0, None if bsecs == 0 else bsecs,
                                   "лимит предупреждений", source)
            out += "\n🚫 Лимит предупреждений достигнут. " + (r.text if r.ok else "Не удалось исключить: " + r.text)
        return OK(out)

    def unwarn(self, chat_id, target, by, count=1, number=None, all_=False, source="chat") -> Result:
        ws = self.active_warns(chat_id, target)
        if not ws:
            return ERR("У пользователя нет предупреждений.")
        if by and self.rank_of(chat_id, by) < self.rank_of(chat_id, target):
            return ERR("Недостаточно прав.")
        if all_:
            ids = [w["id"] for w in ws]
        elif number:
            if not 1 <= number <= len(ws):
                return ERR("Нет предупреждения с таким номером.")
            ids = [ws[number - 1]["id"]]
        else:
            ids = [w["id"] for w in ws][-count:]
        for i in ids:
            self.db.execute("DELETE FROM warns WHERE id=?", (i,))
        self.audit(chat_id, by, "-варн", f"{target} x{len(ids)}", source)
        return OK(f"✅ Снято предупреждений: {len(ids)}. У {self.m(target)} осталось {len(ws) - len(ids)}.")

    # ---------- мут ----------
    async def _perms_default(self, chat_id) -> ChatPermissions:
        try:
            ch = await self.bot.get_chat(chat_id)
            if ch.permissions:
                return ch.permissions
        except Exception:
            pass
        return ChatPermissions(can_send_messages=True, can_send_audios=True, can_send_documents=True,
                               can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
                               can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True,
                               can_add_web_page_previews=True)

    async def mute(self, chat_id, target, by, secs=None, reason="", source="chat") -> Result:
        bad = self._check(chat_id, by, target)
        if bad:
            return bad
        if secs is None:
            secs = self.get(chat_id, "mute_default_secs") or FOREVER
        until = 0 if secs == FOREVER else now() + max(secs, 60)
        perms = ChatPermissions(**{k: False for k in (
            "can_send_messages", "can_send_audios", "can_send_documents", "can_send_photos", "can_send_videos",
            "can_send_video_notes", "can_send_voice_notes", "can_send_polls", "can_send_other_messages",
            "can_add_web_page_previews")})
        ok, err = await self._tg(self.bot.restrict_chat_member(chat_id, target, permissions=perms,
                                                               until_date=until or None))
        if not ok:
            return ERR(f"Telegram отказал: {esc(err)}")
        self.db.execute("INSERT INTO mutes(chat_id,user_id,by_id,reason,until,created_at) VALUES(?,?,?,?,?,?)"
                        " ON CONFLICT(chat_id,user_id) DO UPDATE SET by_id=excluded.by_id,reason=excluded.reason,"
                        " until=excluded.until, created_at=excluded.created_at",
                        (chat_id, target, by, reason, until, now()))
        self.audit(chat_id, by, "мут", f"{target} {fmt_duration(secs)} {reason}"[:200], source)
        out = f"🔇 {self.m(target)} лишён права писать {fmt_until(until)}.{self._tag(chat_id, by)}"
        if reason:
            out += f"\nПричина: {esc(reason)}"
        return OK(out)

    async def unmute(self, chat_id, target, by, source="chat") -> Result:
        if by and self.rank_of(chat_id, by) < self.rank_of(chat_id, target):
            return ERR("Недостаточно прав.")
        perms = await self._perms_default(chat_id)
        ok, err = await self._tg(self.bot.restrict_chat_member(chat_id, target, permissions=perms))
        if not ok:
            return ERR(f"Telegram отказал: {esc(err)}")
        self.db.execute("DELETE FROM mutes WHERE chat_id=? AND user_id=?", (chat_id, target))
        self.audit(chat_id, by, "-мут", str(target), source)
        return OK(f"🔊 С {self.m(target)} снят мут.")

    # ---------- бан / кик ----------
    async def ban(self, chat_id, target, by, secs=None, reason="", source="chat") -> Result:
        bad = self._check(chat_id, by, target)
        if bad:
            return bad
        if secs is None:
            secs = self.get(chat_id, "ban_default_secs") or FOREVER
        until = 0 if secs == FOREVER else now() + max(secs, 60)
        ok, err = await self._tg(self.bot.ban_chat_member(chat_id, target, until_date=until or None))
        if not ok:
            return ERR(f"Telegram отказал: {esc(err)}")
        self.db.execute("INSERT INTO bans(chat_id,user_id,by_id,reason,until,created_at) VALUES(?,?,?,?,?,?)"
                        " ON CONFLICT(chat_id,user_id) DO UPDATE SET by_id=excluded.by_id,reason=excluded.reason,"
                        " until=excluded.until, created_at=excluded.created_at",
                        (chat_id, target, by, reason, until, now()))
        self.db.execute("UPDATE members SET left=1 WHERE chat_id=? AND user_id=?", (chat_id, target))
        self.audit(chat_id, by, "бан", f"{target} {fmt_duration(secs)} {reason}"[:200], source)
        out = f"🚫 {self.m(target)} заблокирован {fmt_until(until)}.{self._tag(chat_id, by)}"
        if reason:
            out += f"\nПричина: {esc(reason)}"
        return OK(out)

    async def unban(self, chat_id, target, by, source="chat") -> Result:
        row = self.db.one("SELECT by_id FROM bans WHERE chat_id=? AND user_id=?", (chat_id, target))
        if row and by and self.rank_of(chat_id, by) < self.rank_of(chat_id, row["by_id"]):
            return ERR("Бан выдал модератор выше вас по рангу.")
        ok, err = await self._tg(self.bot.unban_chat_member(chat_id, target, only_if_banned=True))
        if not ok:
            return ERR(f"Telegram отказал: {esc(err)}")
        self.db.execute("DELETE FROM bans WHERE chat_id=? AND user_id=?", (chat_id, target))
        self.audit(chat_id, by, "разбан", str(target), source)
        return OK(f"✅ {self.m(target)} разблокирован.")

    async def _remove(self, chat_id, target, by, ban=False, reason="", source="chat") -> Result:
        """Исключение без записи в банлист (кик): бан + мгновенный разбан."""
        if by:
            bad = self._check(chat_id, by, target)
            if bad:
                return bad
        ok, err = await self._tg(self.bot.ban_chat_member(chat_id, target))
        if not ok:
            return ERR(f"Telegram отказал: {esc(err)}")
        await self._tg(self.bot.unban_chat_member(chat_id, target, only_if_banned=True))
        self.db.execute("UPDATE members SET left=1 WHERE chat_id=? AND user_id=?", (chat_id, target))
        self.audit(chat_id, by, "кик", f"{target} {reason}"[:200], source)
        return OK(f"👢 {self.m(target)} исключён из чата.{self._tag(chat_id, by)}")

    async def kick(self, chat_id, target, by, reason="", source="chat") -> Result:
        return await self._remove(chat_id, target, by, reason=reason, source=source)

    async def amnesty(self, chat_id, by, source="chat") -> Result:
        rows = self.db.query("SELECT user_id FROM bans WHERE chat_id=?", (chat_id,))
        n = 0
        for r in rows:
            ok, _ = await self._tg(self.bot.unban_chat_member(chat_id, r["user_id"], only_if_banned=True))
            if ok:
                n += 1
        self.db.execute("DELETE FROM bans WHERE chat_id=?", (chat_id,))
        self.audit(chat_id, by, "амнистия", f"{n}", source)
        return OK(f"🕊 Амнистия: разблокировано {n}.")

    # ---------- чистка по активности ----------
    def inactive_ids(self, chat_id, secs=None, count=None, only_silent=False, newcomers_secs=None,
                     active_secs=None, min_msgs=None, period=None):
        base = "SELECT m.user_id FROM members m JOIN users u ON u.user_id=m.user_id " \
               "WHERE m.chat_id=? AND m.left=0 AND u.is_bot=0 AND m.user_id NOT IN " \
               "(SELECT user_id FROM ranks WHERE chat_id=? AND rank>0)"
        args = [chat_id, chat_id]
        t = now()
        if secs is not None:
            base += " AND m.last_seen < ?"
            args.append(t - secs)
            rows = self.db.query(base, args)
        elif count is not None:
            rows = self.db.query(base + " ORDER BY m.last_seen ASC LIMIT ?", args + [count])
        elif only_silent:
            base += " AND m.msg_count=0"
            if period:
                base += " AND m.joined_at < ?"
                args.append(t - period)
            rows = self.db.query(base, args)
        elif newcomers_secs is not None:
            rows = self.db.query(base + " AND m.joined_at >= ?", args + [t - newcomers_secs])
        elif active_secs is not None:
            rows = self.db.query(base + " AND m.last_seen >= ?", args + [t - active_secs])
        elif min_msgs is not None:
            since = day_ago(max(1, (period or 604800) // 86400))
            rows = self.db.query(
                base + " AND COALESCE((SELECT SUM(cnt) FROM msg_daily d WHERE d.chat_id=m.chat_id"
                       " AND d.user_id=m.user_id AND d.day>=?),0) < ?", args + [since, min_msgs])
        else:
            rows = []
        return [r["user_id"] for r in rows]

    async def kick_many(self, chat_id, ids, by, source="chat") -> Result:
        done = 0
        for uid in ids:
            if by and self.rank_of(chat_id, by) <= self.rank_of(chat_id, uid):
                continue
            ok, _ = await self._tg(self.bot.ban_chat_member(chat_id, uid))
            if ok:
                await self._tg(self.bot.unban_chat_member(chat_id, uid, only_if_banned=True))
                self.db.execute("UPDATE members SET left=1 WHERE chat_id=? AND user_id=?", (chat_id, uid))
                done += 1
        self.audit(chat_id, by, "чистка", f"исключено {done} из {len(ids)}", source)
        return OK(f"🧹 Исключено: {done} из {len(ids)}.")

    # ---------- закрыть / открыть чат ----------
    async def set_chat_open(self, chat_id, open_: bool, by, source="chat") -> Result:
        if open_:
            raw = self.db.scalar("SELECT value FROM settings WHERE chat_id=? AND key='perms_backup'", (chat_id,))
            perms = ChatPermissions.model_validate_json(raw) if raw else await self._perms_default(chat_id)
            ok, err = await self._tg(self.bot.set_chat_permissions(chat_id, perms))
            if not ok:
                return ERR(f"Telegram отказал: {esc(err)}")
            self.put(chat_id, "chat_open", 1)
            self.audit(chat_id, by, "+чат", "", source)
            return OK("🔓 Чат открыт: участники снова могут писать.")
        try:
            ch = await self.bot.get_chat(chat_id)
            cur = ch.permissions
        except Exception:
            cur = None
        if self.get_raw(chat_id, "chat_open") != "0" and cur:
            self.put(chat_id, "perms_backup", cur.model_dump_json(exclude_none=True))
        closed = ChatPermissions(**{k: False for k in (
            "can_send_messages", "can_send_audios", "can_send_documents", "can_send_photos", "can_send_videos",
            "can_send_video_notes", "can_send_voice_notes", "can_send_polls", "can_send_other_messages",
            "can_add_web_page_previews")})
        ok, err = await self._tg(self.bot.set_chat_permissions(chat_id, closed))
        if not ok:
            return ERR(f"Telegram отказал: {esc(err)}")
        self.put(chat_id, "chat_open", 0)
        self.audit(chat_id, by, "-чат", "", source)
        return OK("🔒 Чат закрыт: писать могут только администраторы.")

    def get_raw(self, chat_id, key):
        return self.db.scalar("SELECT value FROM settings WHERE chat_id=? AND key=?", (chat_id, key))

    # ---------- статистика ----------
    def top(self, chat_id, period: str, limit: int = 10):
        if period == "вся":
            return self.db.query(
                "SELECT m.user_id, m.msg_count AS cnt FROM members m WHERE m.chat_id=? AND m.left=0"
                " ORDER BY m.msg_count DESC LIMIT ?", (chat_id, limit))
        days = {"сутки": 0, "неделя": 6, "месяц": 29}[period]
        return self.db.query(
            "SELECT d.user_id, SUM(d.cnt) AS cnt FROM msg_daily d JOIN members m ON m.chat_id=d.chat_id"
            " AND m.user_id=d.user_id AND m.left=0 WHERE d.chat_id=? AND d.day>=? GROUP BY d.user_id"
            " ORDER BY cnt DESC LIMIT ?", (chat_id, day_ago(days), limit))

    def msgs_in(self, chat_id, uid, days: int) -> int:
        return self.db.scalar("SELECT SUM(cnt) FROM msg_daily WHERE chat_id=? AND user_id=? AND day>=?",
                              (chat_id, uid, day_ago(days)), 0)

    # ---------- данные для панели ----------
    def chat_overview(self, chat_id: int) -> dict:
        d = self.db
        row = d.one("SELECT * FROM chats WHERE chat_id=?", (chat_id,))
        return {
            "chat_id": chat_id, "title": row["title"], "code": row["code"],
            "members": d.scalar("SELECT COUNT(*) FROM members WHERE chat_id=? AND left=0", (chat_id,), 0),
            "msgs_today": d.scalar("SELECT SUM(cnt) FROM msg_daily WHERE chat_id=? AND day=?",
                                   (chat_id, today_str()), 0),
            "msgs_week": d.scalar("SELECT SUM(cnt) FROM msg_daily WHERE chat_id=? AND day>=?",
                                  (chat_id, day_ago(6)), 0),
            "bans": d.scalar("SELECT COUNT(*) FROM bans WHERE chat_id=?", (chat_id,), 0),
            "mutes": d.scalar("SELECT COUNT(*) FROM mutes WHERE chat_id=?", (chat_id,), 0),
            "warns": d.scalar("SELECT COUNT(*) FROM warns WHERE chat_id=? AND (expires_at=0 OR expires_at>?)",
                              (chat_id, now()), 0),
            "staff": d.scalar("SELECT COUNT(*) FROM ranks WHERE chat_id=? AND rank>0", (chat_id,), 0),
        }

    def members_list(self, chat_id: int, q: str = "", limit: int = 50, offset: int = 0) -> dict:
        d = self.db
        like = f"%{q.lower()}%"
        where = "m.chat_id=? AND m.left=0"
        args = [chat_id]
        if q:
            where += " AND (LOWER(COALESCE(u.username,'')) LIKE ? OR LOWER(COALESCE(u.first_name,'')) LIKE ?" \
                     " OR LOWER(COALESCE(u.last_name,'')) LIKE ? OR CAST(u.user_id AS TEXT) LIKE ?)"
            args += [like, like, like, like]
        total = d.scalar(f"SELECT COUNT(*) FROM members m JOIN users u ON u.user_id=m.user_id WHERE {where}", args, 0)
        rows = d.query(
            f"SELECT m.user_id, u.username, u.first_name, u.last_name, u.is_bot, m.msg_count, m.last_seen,"
            f" m.joined_at, COALESCE(r.rank,0) AS rank,"
            f" (SELECT COUNT(*) FROM warns w WHERE w.chat_id=m.chat_id AND w.user_id=m.user_id"
            f"  AND (w.expires_at=0 OR w.expires_at>{now()})) AS warns,"
            f" (SELECT until FROM mutes mu WHERE mu.chat_id=m.chat_id AND mu.user_id=m.user_id) AS muted"
            f" FROM members m JOIN users u ON u.user_id=m.user_id"
            f" LEFT JOIN ranks r ON r.chat_id=m.chat_id AND r.user_id=m.user_id"
            f" WHERE {where} ORDER BY rank DESC, m.last_seen DESC LIMIT ? OFFSET ?", args + [limit, offset])
        items = [{
            "user_id": r["user_id"], "name": full_name(r["first_name"], r["last_name"], r["username"], r["user_id"]),
            "username": r["username"], "is_bot": bool(r["is_bot"]), "msgs": r["msg_count"],
            "last_seen": r["last_seen"], "joined_at": r["joined_at"], "rank": r["rank"], "warns": r["warns"],
            "muted": (r["muted"] is not None), "muted_until": r["muted"] or 0,
        } for r in rows]
        return {"total": total, "items": items}

    def punishments(self, chat_id: int) -> dict:
        d = self.db

        def rows(sql, args):
            return [dict(r) for r in d.query(sql, args)]
        bans = rows("SELECT user_id, by_id, reason, until, created_at FROM bans WHERE chat_id=?"
                    " ORDER BY created_at DESC", (chat_id,))
        mutes = rows("SELECT user_id, by_id, reason, until, created_at FROM mutes WHERE chat_id=?"
                     " ORDER BY created_at DESC", (chat_id,))
        warns = rows("SELECT id, user_id, by_id, reason, created_at, expires_at FROM warns WHERE chat_id=?"
                     " AND (expires_at=0 OR expires_at>?) ORDER BY id DESC", (chat_id, now()))
        for lst in (bans, mutes, warns):
            for r in lst:
                r["name"] = self.name(r["user_id"])
                r["by_name"] = self.name(r["by_id"]) if r.get("by_id") else "система"
        return {"bans": bans, "mutes": mutes, "warns": warns}

    # ---------- фоновое обслуживание ----------
    def maintenance(self) -> None:
        t = now()
        self.db.execute("DELETE FROM mutes WHERE until>0 AND until<?", (t,))
        self.db.execute("DELETE FROM bans WHERE until>0 AND until<?", (t,))
        self.db.execute("DELETE FROM warns WHERE expires_at>0 AND expires_at<?", (t,))
        self.db.execute("DELETE FROM web_tokens WHERE expires_at<?", (t,))
        self.db.execute("DELETE FROM web_sessions WHERE expires_at<?", (t,))
        self.db.execute("DELETE FROM leaves WHERE at<?", (t - 7 * 86400,))
        self.db.execute("UPDATE votes SET closed=1 WHERE closed=0 AND created_at<?", (t - 86400,))
