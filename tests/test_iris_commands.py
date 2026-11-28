"""Команды, ранги, наказания и веб-API без Telegram: подставной бот + реальные обработчики."""
import asyncio
from types import SimpleNamespace as NS

from fastapi.testclient import TestClient

from crymoder import commands as handlers
from crymoder.config import Config
from crymoder.core import Core
from crymoder.db import DB
from crymoder.web.main import create_app

CHAT = -1001
CALLS = []
CONFIG = Config("1:x", frozenset(), ":memory:", None, "INFO", public_url="http://panel.test")


class FakeBot:
    def __getattr__(self, name):
        async def f(*a, **kw):
            CALLS.append((name, a, kw))
            if name == "get_chat":
                return NS(permissions=None, type="supergroup")
            if name == "get_chat_administrators":
                return list(ADMINS)
            if name == "create_chat_invite_link":
                return NS(invite_link="https://t.me/+abc")
            return True
        return f


ADMINS = []


def U(i, un=None, bot=False):
    return NS(id=i, username=un, first_name=un or f"u{i}", last_name=None, is_bot=bot)


_mid = [100]


def M(text, uid, reply=None, chat=CHAT, ctype="supergroup"):
    _mid[0] += 1
    return NS(text=text, caption=None, from_user=U(uid, {1: "boss", 2: "mod", 3: "user", 4: "victim"}.get(uid)),
              chat=NS(id=chat, type=ctype, title="Test chat"), message_id=_mid[0], reply_to_message=reply,
              entities=None, caption_entities=None, is_topic_message=False, message_thread_id=None)


def sent_texts():
    return [kw.get("text") or (a[1] if len(a) > 1 else "") for n, a, kw in CALLS if n == "send_message"]


def check(cond, what):
    print(("OK   " if cond else "FAIL ") + what)
    if not cond:
        check.bad += 1


check.bad = 0


async def say(core, bot, text, uid, reply=None, **kw):
    CALLS.clear()
    m = M(text, uid, reply, **kw)
    handled = await handlers.handle_message(core, bot, m)
    return handled, " | ".join(str(t) for t in sent_texts())


async def main():
    db = DB(":memory:")
    bot = FakeBot()
    core = Core(db, bot, CONFIG)
    core.me_id = 999
    core.me_username = "crymoder_bot"
    ADMINS[:] = [NS(status="creator", user=U(1, "boss"))]
    for i, n in ((1, "boss"), (2, "mod"), (3, "user"), (4, "victim")):
        core.reg_user(U(i, n))
    core.ensure_chat(CHAT, "Test chat")
    await core.sync_creator(CHAT)
    check(core.rank_of(CHAT, 1) == 5, "создатель получает 5 ранг")

    # ранги
    h, t = await say(core, bot, "+модер 2 @mod", 1)
    check(core.rank_of(CHAT, 2) == 2, "создатель назначает 2 ранг: " + t[:60])
    h, t = await say(core, bot, "+модер 3 @user", 2)
    check(core.rank_of(CHAT, 3) == 0, "модератор не может назначить ранг выше своего")
    h, t = await say(core, bot, "кто админ", 3)
    check("boss" in t and "mod" in t, "кто админ показывает модераторов")

    # доступ: обычный участник не банит; без префикса команда молчит
    h, t = await say(core, bot, "бан @victim", 3)
    check(core.db.one("SELECT 1 FROM bans") is None, "участник без прав не может банить")
    h, t = await say(core, bot, "!бан @victim", 3)
    check("Недостаточно прав" in t, "с префиксом получает отказ")

    # варны до лимита
    for i in range(3):
        h, t = await say(core, bot, "варн @victim\nфлуд", 2)
    check(any(n == "restrict_chat_member" for n, *_ in CALLS) and core.db.one("SELECT 1 FROM mutes WHERE user_id=4"),
          "3 варна → мут (действие по умолчанию, как в CRYMODER)")
    check(len(core.active_warns(CHAT, 4)) == 0, "варны очищены после наказания")
    await say(core, bot, "размут @victim", 2)

    # мут со сроком и причиной
    h, t = await say(core, bot, "мут 2 часа @victim\nспам", 2)
    r = core.db.one("SELECT * FROM mutes WHERE user_id=4")
    check(r and 7000 < r["until"] - r["created_at"] < 7400 and r["reason"] == "спам", "мут 2 часа с причиной")
    h, t = await say(core, bot, "размут @victim", 2)
    check(core.db.one("SELECT 1 FROM mutes") is None, "размут снимает мут")

    # бан ответом на сообщение
    reply = M("что-то", 4)
    h, t = await say(core, bot, "бан навсегда\nрейдер", 2, reply=reply)
    r = core.db.one("SELECT * FROM bans WHERE user_id=4")
    check(r and r["until"] == 0 and r["reason"] == "рейдер", "бан ответом, навсегда")
    h, t = await say(core, bot, "!причина @victim", 2)
    check("рейдер" in t, "причина бана")
    h, t = await say(core, bot, "разбан @victim", 2)
    check(core.db.one("SELECT 1 FROM bans") is None, "разбан")

    # нельзя наказать равного/старшего
    h, t = await say(core, bot, "!бан @boss", 2)
    check("Недостаточно прав" in t or "не ниже" in t, "нельзя банить старшего")

    # доступ команд
    h, t = await say(core, bot, "дк бан 4", 1)
    check(core.required_rank(CHAT, "бан") == 4, "дк бан 4 меняет ранг")
    h, t = await say(core, bot, "!бан @victim", 2)
    check("Недостаточно прав" in t, "после дк 2 ранг больше не банит")
    h, t = await say(core, bot, "-дк варн", 1)
    check(not core.has_access(CHAT, 2, "варн"), "-дк отключает команду")
    h, t = await say(core, bot, "!сброс команд", 1)
    check(core.required_rank(CHAT, "бан") == 2, "сброс команд")
    h, t = await say(core, bot, "+лдк бан @user", 1)
    check(core.has_access(CHAT, 3, "бан"), "личный доступ")
    core.set_personal(CHAT, 3, "бан", None, 1)

    # настройки
    h, t = await say(core, bot, "+правила\nНе спамить", 1)
    h, t = await say(core, bot, "правила", 3)
    check("Не спамить" in t, "правила")
    h, t = await say(core, bot, "-ссылки", 1)
    check(core.get(CHAT, "allow_sites") == 0, "-ссылки")
    h, t = await say(core, bot, "варны лимит 5", 1)
    check(core.get(CHAT, "warn_limit") == 5, "варны лимит")
    h, t = await say(core, bot, "-чат", 1)
    check(any(n == "set_chat_permissions" for n, *_ in CALLS), "закрытие чата")
    h, t = await say(core, bot, "+чат", 1)

    # чистка
    core.db.execute("INSERT INTO members(chat_id,user_id,first_seen,last_seen,msg_count,joined_at) VALUES(?,?,?,?,?,?)",
                    (CHAT, 50, 1000, 1000, 0, 1000))
    core.reg_user(U(50, "old"))
    h, t = await say(core, bot, "кик неактив 2 дня", 1)
    check("Исключено: 1" in t, "кик неактив: " + t[:50])
    h, t = await say(core, bot, "кик неактив 1 час", 1)
    check("24 часа" in t, "минимум 24 часа")

    # статистика и репутация
    h, t = await say(core, bot, "стата", 3)
    check(h, "стата отвечает")
    target = M("привет", 3)
    h, t = await say(core, bot, "++", 2, reply=target)
    check("Репутация" in t, "плюсы ответом: " + t[:60])
    h, t = await say(core, bot, "профиль", 3)
    check("Репутация" in t, "профиль")

    # приветствие
    check("Привет, Вася" in handlers.render_greeting("Привет, {имя}! Рад{ж|м|мн}", "Вася", "м")
          and handlers.render_greeting("Рад{а|(а)|ы}", "x", "ж") == "Рад" + "а", "рендер приветствия")

    # веб
    core.db.execute("INSERT INTO web_tokens(token,user_id,expires_at) VALUES('tok',1,9999999999)")
    web = TestClient(create_app(core), follow_redirects=False)
    check(web.get("/api/me").status_code == 401, "веб: без входа 401")
    check(web.get("/login?t=bad").status_code == 400, "веб: плохой токен")
    r = web.get("/login?t=tok")
    check(r.status_code == 303 and "sid" in r.cookies, "веб: вход по токену")
    check(web.get("/login?t=tok").status_code == 400, "веб: токен одноразовый")
    me = web.get("/api/me").json()
    check(me["chats"] and me["chats"][0]["chat_id"] == CHAT, "веб: список чатов")
    for p in ("overview", "members", "punishments", "staff", "audit", "settings", "access"):
        check(web.get(f"/api/chats/{CHAT}/{p}").status_code == 200, f"веб: GET {p}")
    r = web.post(f"/api/chats/{CHAT}/action", json={"action": "ban", "user_id": 3, "period": "1 день", "reason": "тест"})
    check(r.json()["ok"], "веб: бан")
    check(core.db.one("SELECT 1 FROM bans WHERE user_id=3") is not None, "веб: бан записан в базу")
    r = web.post(f"/api/chats/{CHAT}/action", json={"action": "ban", "user_id": 3, "period": "абракадабра"})
    check(r.status_code == 400, "веб: плохой срок → 400")
    r = web.post(f"/api/chats/{CHAT}/action", json={"action": "unban", "user_id": 3})
    check(r.json()["ok"], "веб: разбан")
    r = web.post(f"/api/chats/{CHAT}/settings", json={"key": "warn_limit", "value": 7})
    check(core.get(CHAT, "warn_limit") == 7, "веб: настройка")
    r = web.post(f"/api/chats/{CHAT}/settings", json={"key": "warn_limit", "value": 0})
    check(not r.json()["ok"], "веб: валидация лимита")
    r = web.post(f"/api/chats/{CHAT}/access", json={"code": "бан", "rank": 3})
    check(core.required_rank(CHAT, "бан") == 3, "веб: доступ команд")
    r = web.post(f"/api/chats/{CHAT}/access", json={"code": "дк", "rank": 6})
    check(r.status_code == 400, "веб: нельзя отключить управление доступом")
    r = web.post(f"/api/chats/{CHAT}/clean/preview", json={"mode": "inactive", "value": 5, "unit": "hours"})
    check(r.status_code == 400, "веб: чистка <24ч запрещена")
    r = web.post(f"/api/chats/{CHAT}/action", content="x=1", headers={"content-type": "text/plain"})
    check(r.status_code == 415, "веб: не-JSON отклонён (CSRF)")
    # права модератора 2 ранга в вебе
    core.db.execute("INSERT INTO web_tokens(token,user_id,expires_at) VALUES('t2',2,9999999999)")
    w2 = TestClient(create_app(core), follow_redirects=False)
    w2.get("/login?t=t2")
    check(w2.post(f"/api/chats/{CHAT}/access", json={"code": "бан", "rank": 0}).status_code == 403,
          "веб: 2 ранг не меняет доступ команд")
    r = w2.post(f"/api/chats/{CHAT}/action", json={"action": "ban", "user_id": 1})
    check(not r.json().get("ok", True) if r.status_code == 200 else r.status_code == 403, "веб: нельзя банить старшего")
    core.db.execute("INSERT INTO web_tokens(token,user_id,expires_at) VALUES('t3',3,9999999999)")
    w3 = TestClient(create_app(core), follow_redirects=False)
    w3.get("/login?t=t3")
    check(w3.get(f"/api/chats/{CHAT}/members").status_code == 403, "веб: обычный участник без доступа")

    # панель из лички
    h, t = await say(core, bot, "панель", 1, chat=1, ctype="private")
    check(any("login?t=" in str(x) for x in sent_texts()), "«панель» присылает ссылку входа")

    await extra_checks(core, bot)

    print(f"\nПровалено: {check.bad}")
    assert check.bad == 0, f"провалено проверок: {check.bad}"


async def extra_checks(core, bot):
    """Совместимость со слеш-командами CRYMODER и новые возможности единого бота."""
    core.db.execute("DELETE FROM warns")
    core.db.execute("DELETE FROM mutes")
    core.db.execute("DELETE FROM bans")
    core.reset_access(CHAT, 1)                      # доступ команд — по умолчанию
    core.put(CHAT, "warn_limit", 3)
    core.put(CHAT, "warn_action", "mute")

    # --- слеш-команды и компактные сроки
    h, t = await say(core, bot, "/mute 10m тест", 2, reply=M("x", 4))
    r = core.db.one("SELECT * FROM mutes WHERE user_id=4")
    check(r and 540 < r["until"] - r["created_at"] < 660 and r["reason"] == "тест", "/mute 10m работает как в CRYMODER")
    await say(core, bot, "/unmute", 2, reply=M("x", 4))
    check(core.db.one("SELECT 1 FROM mutes") is None, "/unmute")
    h, t = await say(core, bot, "/ban 777777 реклама", 2)
    check(core.db.one("SELECT * FROM bans WHERE user_id=777777"), "/ban по числовому ID")
    await say(core, bot, "/unban 777777", 2)
    check(core.db.one("SELECT 1 FROM bans") is None, "/unban по ID")
    h, t = await say(core, bot, "/warn@crymoder_bot спам", 2, reply=M("x", 4))
    check(len(core.active_warns(CHAT, 4)) == 1, "/warn@имя_этого_бота выполняется")
    h, t = await say(core, bot, "/warn@other_bot спам", 2, reply=M("x", 4))
    check(not h and len(core.active_warns(CHAT, 4)) == 1, "команда, адресованная другому боту, игнорируется")
    h, t = await say(core, bot, "/warns", 3)
    check("нет предупреждений" in t, "/warns участника показывает его собственные варны")
    h, t = await say(core, bot, "/warns", 2, reply=M("x", 4))
    check("1/3" in t, "/warns модератора в ответ на сообщение показывает чужие")
    await say(core, bot, "/unwarn", 2, reply=M("x", 4))
    check(len(core.active_warns(CHAT, 4)) == 0, "/unwarn снимает предупреждение")
    for _ in range(2):
        await say(core, bot, "/warn", 2, reply=M("x", 4))
    await say(core, bot, "/resetwarns", 2, reply=M("x", 4))
    check(len(core.active_warns(CHAT, 4)) == 0, "/resetwarns снимает все")
    h, t = await say(core, bot, "/del", 2, reply=M("x", 4))
    check(any(n in ("delete_message", "delete_messages") for n, *_ in CALLS), "/del удаляет сообщение")
    h, t = await say(core, bot, "/id", 3, reply=M("x", 4))
    check(str(CHAT) in t and "4" in t, "/id показывает ID чата и автора ответа")
    h, t = await say(core, bot, "/id", 3, chat=3, ctype="private")
    check(h and "3" in t and str(CHAT) not in t, "/id работает в личке")

    # --- действие при лимите варнов: бан и кик
    await say(core, bot, "варны действие бан", 1)
    check(core.get(CHAT, "warn_action") == "ban", "варны действие бан")
    for _ in range(3):
        await say(core, bot, "/warn", 2, reply=M("x", 4))
    check(core.db.one("SELECT 1 FROM bans WHERE user_id=4"), "лимит варнов → бан")
    await say(core, bot, "разбан @victim", 2)
    await say(core, bot, "варны действие кик", 1)
    CALLS.clear()
    for _ in range(3):
        await say(core, bot, "/warn", 2, reply=M("x", 4))
    names = [n for n, *_ in CALLS]
    check("ban_chat_member" in names and "unban_chat_member" in names
          and core.db.one("SELECT 1 FROM bans") is None, "лимит варнов → кик без записи в банлист")
    await say(core, bot, "варны действие мут", 1)
    await say(core, bot, "варны чс 3 дня", 1)
    check(core.get(CHAT, "warn_action") == "ban" and core.get(CHAT, "warn_ban_secs") == 259200, "варны чс 3 дня")
    await say(core, bot, "варны действие мут", 1)

    # --- валидация числовых настроек
    check(not core.set_setting(CHAT, "flood_limit", 1, 1, "chat").ok, "flood_limit вне диапазона отклоняется")
    check(core.set_setting(CHAT, "flood_limit", 5, 1, "chat").ok and core.get(CHAT, "flood_limit") == 5, "flood_limit в диапазоне")

    # --- фильтр слов и панель настроек через команды
    h, t = await say(core, bot, "/addword Казино", 2)
    check(core.words(CHAT) == ["казино"], "/addword нормализует слово")
    h, t = await say(core, bot, "/words", 2)
    check("казино" in t, "/words показывает список")
    h, t = await say(core, bot, "/delword казино", 2)
    check(core.words(CHAT) == [], "/delword")
    h, t = await say(core, bot, "/addword казино", 3)
    check(core.words(CHAT) == [] and "Недостаточно прав" in t, "участник не может менять фильтр слов")
    h, t = await say(core, bot, "/settings", 2)
    sent = [kw for n, a, kw in CALLS if n == "send_message"]
    check(sent and sent[0].get("reply_markup"), "/settings отправляет клавиатуру")
    h, t = await say(core, bot, "/settings", 3)
    check("Недостаточно прав" in t, "участнику /settings недоступен")

    # --- Telegram-админы автоматически получают ранг
    ADMINS[:] = [NS(status="creator", user=U(1, "boss")),
                 NS(status="administrator", user=U(30, "tgadmin"), can_restrict_members=True),
                 NS(status="administrator", user=U(31, "cosmetic"), can_restrict_members=False)]
    await core.sync_admins(CHAT)
    check(core.rank_of(CHAT, 30) == 2, "TG-админ с правом блокировки получает 2 ранг")
    check(core.rank_of(CHAT, 31) == 0, "TG-админ без права блокировки ранга не получает")
    core.reg_user(U(30, "tgadmin"))
    h, t = await say(core, bot, "/warn", 30, reply=M("x", 4))
    check(len(core.active_warns(CHAT, 4)) == 1, "TG-админ может /warn")
    await core.sync_creator(CHAT)
    core.set_rank(CHAT, 3, 1, 1)
    ADMINS[:] = [NS(status="creator", user=U(1, "boss"))]
    await core.sync_admins(CHAT)
    check(core.rank_of(CHAT, 30) == 0, "разжалованный в Telegram админ теряет авто-ранг")
    check(core.rank_of(CHAT, 3) == 1, "вручную выданный ранг не трогается синхронизацией")
    check(core.rank_of(CHAT, 1087968824) == 2, "анонимный админ действует как старший модератор")


def test_commands_and_web():
    asyncio.run(main())
