"""Ядро: все действия модерации. Используется и командами бота, и веб-панелью,
поэтому права проверяются здесь, а не в обработчиках."""
import logging
import random
import string
import time
from dataclasses import dataclass

from aiogram.types import ChatPermissions

from . import registry
from .utils import spawn
from .utils import FOREVER, esc, fmt_duration, fmt_until, full_name, mention, now, today_str, day_ago

RANK_NAMES = {0: "участник", 1: "младший модератор", 2: "старший модератор",
              3: "младший администратор", 4: "старший администратор", 5: "создатель"}
DISABLED = 6
ANON_ADMIN_ID = 1087968824   # от этого «пользователя» Telegram приходят сообщения анонимных админов
ANON_ADMIN_RANK = 2          # анонимный админ действует как старший модератор
TG_ADMIN_RANK = 2            # ранг, который получают Telegram-админы с правом блокировки участников

log = logging.getLogger("crymoder.core")

# ключ: (тип, по умолчанию, описание)
SETTINGS = {
    "warn_limit": ("int", 3, "Лимит предупреждений до наказания"),
    "warn_action": ("choice:mute,ban,kick", "mute", "Наказание при лимите предупреждений"),
    "warn_mute_secs": ("secs", 86400, "Срок мута при лимите варнов"),
    "warn_ban_secs": ("secs", 0, "Срок бана при лимите варнов (0 — навсегда)"),
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
    # автомодерация
    "antiflood": ("bool", 1, "Антифлуд: мут за поток сообщений"),
    "flood_limit": ("int", 6, "Антифлуд: сообщений в окне"),
    "flood_window": ("int", 8, "Антифлуд: окно, секунд"),
    "antilink": ("bool", 0, "Автомодерация: удалять любые ссылки и предупреждать"),
    "antiinvite": ("bool", 1, "Автомодерация: удалять инвайт-ссылки и предупреждать"),
    "badwords": ("bool", 1, "Автомодерация: фильтр запрещённых слов"),
    "captcha": ("bool", 1, "Капча для новых участников"),
    "captcha_timeout": ("int", 120, "Капча: время на прохождение, секунд"),
    "delete_service": ("bool", 0, "Удалять служебные сообщения о входе и выходе"),
}

# допустимые границы числовых настроек: ключ -> (мин, макс)
RANGES = {"warn_limit": (1, 50), "flood_limit": (3, 30), "flood_window": (3, 60), "captcha_timeout": (30, 600)}


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
    def __init__(self, db, bot=None, config=None):
        self.db = db
        self.bot = bot
        self.config = config
        self.me_id = 0
        self.me_username = ""
        self._admin_sync: dict[int, float] = {}
        self._words: dict[int, list[str]] = {}

    @property
    def super_admins(self) -> frozenset:
        return self.config.super_admins if self.config else frozenset()

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

    async def sync_admins(self, chat_id: int) -> None:
        """Подтягивает администраторов из Telegram: создатель получает 5 ранг, а админы с правом
        блокировки участников — 2 ранг (если ранга у них ещё нет). Так команды, доступные админам
        раньше, продолжают работать. Автоматически выданный 2 ранг снимается, когда админ разжалован."""
        self._admin_sync[chat_id] = time.monotonic()
        try:
            admins = await self.bot.get_chat_administrators(chat_id)
        except Exception:
            return
        tg_mods = set()
        for a in admins:
            status = getattr(a, "status", "")
            if status == "creator":
                self.reg_user(a.user)
                old = self.creator_of(chat_id)
                self.db.execute("UPDATE chats SET creator_id=? WHERE chat_id=?", (a.user.id, chat_id))
                if old != a.user.id and self.rank_of(chat_id, a.user.id) < 5:
                    self._put_rank(chat_id, a.user.id, 5, 0)
            elif status == "administrator" and getattr(a, "can_restrict_members", False) and not a.user.is_bot:
                self.reg_user(a.user)
                tg_mods.add(a.user.id)
                if self.rank_of(chat_id, a.user.id) == 0:
                    self._put_rank(chat_id, a.user.id, TG_ADMIN_RANK, 0)
        for r in self.db.query("SELECT user_id FROM ranks WHERE chat_id=? AND rank=? AND appointed_by=0",
                               (chat_id, TG_ADMIN_RANK)):
            if r["user_id"] not in tg_mods:
                self._put_rank(chat_id, r["user_id"], 0, 0)

    async def sync_creator(self, chat_id: int) -> None:
        await self.sync_admins(chat_id)

    async def maybe_sync_admins(self, chat_id: int, min_interval: float = 600) -> None:
        """Синхронизация не чаще раза в min_interval секунд (чтобы не долбить Telegram API)."""
        last = self._admin_sync.get(chat_id)
        if last is None or time.monotonic() - last >= min_interval:
            await self.sync_admins(chat_id)

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
                if key in RANGES and not RANGES[key][0] <= raw <= RANGES[key][1]:
                    return ERR(f"{SETTINGS[key][2]}: от {RANGES[key][0]} до {RANGES[key][1]}")
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
        log_chat = self.config.log_chat_id if self.config else None
        if log_chat and self.bot:
            try:
                spawn(self._send_log(log_chat, chat_id, by, action, detail, source))
            except RuntimeError:     # нет работающего цикла событий (скрипты, тесты)
                pass

    async def _send_log(self, log_chat, chat_id, by, action, detail, source) -> None:
        title = self.db.scalar("SELECT title FROM chats WHERE chat_id=?", (chat_id,), "") or chat_id
        who = self.m(by) if by else "система"
        text = f"🛡 <b>{esc(action)}</b> · {esc(title)} (<code>{chat_id}</code>)\nКто: {who} [{esc(source)}]"
        if detail:
            text += f"\n{esc(detail)}"
        await self._tg(self.bot.send_message(log_chat, text, disable_notification=True))

    # ---------- ранги ----------
    def rank_of(self, chat_id: int, uid: int) -> int:
        if uid == self.me_id and uid:
            return 5
        if uid == ANON_ADMIN_ID:
            return ANON_ADMIN_RANK
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
            self.db.execute("DELETE FROM warns WHERE chat_id=? AND user_id=?", (chat_id, target))
            r = await self._warn_punish(chat_id, target, source)
            out += "\n🚫 Лимит предупреждений достигнут. " + (r.text if r.ok else "Не удалось наказать: " + r.text)
        return OK(out)

    async def _warn_punish(self, chat_id, target, source) -> Result:
        """Наказание за лимит предупреждений по настройке warn_action (мут / бан / кик)."""
        action = self.get(chat_id, "warn_action")
        reason = "лимит предупреждений"
        if action == "ban":
            bsecs = self.get(chat_id, "warn_ban_secs")
            return await self.ban(chat_id, target, 0, bsecs if bsecs > 0 else FOREVER, reason, source)
        if action == "kick":
            return await self._remove(chat_id, target, 0, reason=reason, source=source)
        return await self.mute(chat_id, target, 0, self.get(chat_id, "warn_mute_secs") or 86400, reason, source)

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
    # ---------- запрещённые слова ----------
    def words(self, chat_id: int) -> list[str]:
        if chat_id not in self._words:
            self._words[chat_id] = [r["word"] for r in self.db.query(
                "SELECT word FROM badwords WHERE chat_id=? ORDER BY word", (chat_id,))]
        return self._words[chat_id]

    def add_word(self, chat_id: int, word: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO badwords(chat_id,word) VALUES(?,?)", (chat_id, word))
        self._words.pop(chat_id, None)

    def del_word(self, chat_id: int, word: str) -> bool:
        had = bool(self.db.one("SELECT 1 FROM badwords WHERE chat_id=? AND word=?", (chat_id, word)))
        self.db.execute("DELETE FROM badwords WHERE chat_id=? AND word=?", (chat_id, word))
        self._words.pop(chat_id, None)
        return had

    # ---------- капча ----------
    def captcha_add(self, chat_id: int, user_id: int, message_id: int, expires_at: int) -> None:
        self.db.execute("INSERT OR REPLACE INTO captcha(chat_id,user_id,message_id,expires_at) VALUES(?,?,?,?)",
                        (chat_id, user_id, message_id, expires_at))

    def captcha_get(self, chat_id: int, user_id: int):
        return self.db.one("SELECT * FROM captcha WHERE chat_id=? AND user_id=?", (chat_id, user_id))

    def captcha_del(self, chat_id: int, user_id: int) -> None:
        self.db.execute("DELETE FROM captcha WHERE chat_id=? AND user_id=?", (chat_id, user_id))

    def captcha_all(self):
        return self.db.query("SELECT * FROM captcha")

    def maintenance(self) -> None:
        t = now()
        self.db.execute("DELETE FROM mutes WHERE until>0 AND until<?", (t,))
        self.db.execute("DELETE FROM bans WHERE until>0 AND until<?", (t,))
        self.db.execute("DELETE FROM warns WHERE expires_at>0 AND expires_at<?", (t,))
        self.db.execute("DELETE FROM web_tokens WHERE expires_at<?", (t,))
        self.db.execute("DELETE FROM web_sessions WHERE expires_at<?", (t,))
        self.db.execute("DELETE FROM leaves WHERE at<?", (t - 7 * 86400,))
        self.db.execute("UPDATE votes SET closed=1 WHERE closed=0 AND created_at<?", (t - 86400,))
