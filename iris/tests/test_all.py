"""Проверка без Telegram: подставной бот + реальные обработчики и веб-API.
Запуск: python -m tests.test_all"""
import asyncio
import sys
from types import SimpleNamespace as NS

from fastapi.testclient import TestClient

from app import handlers
from app.core import Core
from app.db import DB
from app.web.main import create_app

CHAT = -1001
CALLS = []


class FakeBot:
    def __getattr__(self, name):
        async def f(*a, **kw):
            CALLS.append((name, a, kw))
            if name == "get_chat":
                return NS(permissions=None, type="supergroup")
            if name == "get_chat_administrators":
                return [NS(status="creator", user=U(1, "boss"))]
            if name == "create_chat_invite_link":
                return NS(invite_link="https://t.me/+abc")
            return True
        return f


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
    core = Core(db, bot)
    core.me_id = 999
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
    check(any(n == "ban_chat_member" for n, *_ in CALLS), "3 варна → исключение")
    check(len(core.active_warns(CHAT, 4)) == 0, "варны очищены после кика")

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

    print(f"\nПровалено: {check.bad}")
    sys.exit(1 if check.bad else 0)


asyncio.run(main())
