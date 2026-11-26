"""Сквозной тест: хендлеры + подставной Telegram API (сеть не нужна)."""
import asyncio, tempfile, os, time
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import (GetChatMember, SendMessage, GetChat, GetMe, DeleteMessage,
                             RestrictChatMember, BanChatMember, UnbanChatMember)
from aiogram.types import (Update, User, Message, ChatMemberAdministrator, ChatMemberMember,
                           ChatFullInfo, ChatPermissions)

from crymoder.config import Config
from crymoder.db import Database
from crymoder.handlers import automod, captcha, common, moderation, settings

ADMIN, USER, NEWBIE, CHAT = 1, 2, 3, -100500


class Fake(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.mid = 1000

    async def close(self): ...
    async def stream_content(self, *a, **k):
        yield b""

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, GetChatMember):
            u = User(id=method.user_id, is_bot=False, first_name=f"u{method.user_id}")
            if method.user_id == ADMIN:
                return ChatMemberAdministrator.model_construct(status="administrator", user=u, can_restrict_members=True)
            return ChatMemberMember.model_construct(status="member", user=u)
        if isinstance(method, SendMessage):
            self.mid += 1
            return Message.model_validate(
                {"message_id": self.mid, "date": int(time.time()),
                 "chat": {"id": method.chat_id, "type": "supergroup", "title": "T"}, "text": method.text},
                context={"bot": bot})
        if isinstance(method, GetChat):
            return ChatFullInfo.model_construct(id=CHAT, type="supergroup",
                                                permissions=ChatPermissions(can_send_messages=False, can_send_photos=True))
        if isinstance(method, GetMe):
            return User(id=999, is_bot=True, first_name="bot", username="crymoder_bot")
        return True

    def names(self):
        return [type(c).__name__ for c in self.calls]


def upd(uid, text, reply_to=None, n=[0]):
    n[0] += 1
    m = {"message_id": n[0], "date": int(time.time()),
         "chat": {"id": CHAT, "type": "supergroup", "title": "T"},
         "from": {"id": uid, "is_bot": False, "first_name": f"u{uid}"}, "text": text}
    if text.startswith("/"):
        m["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    if reply_to:
        m["reply_to_message"] = {"message_id": 1, "date": int(time.time()),
                                 "chat": m["chat"], "from": {"id": reply_to, "is_bot": False, "first_name": f"u{reply_to}"},
                                 "text": "x"}
    return {"update_id": n[0], "message": m}


async def scenario():
    tmp = tempfile.mkdtemp()
    db = Database(os.path.join(tmp, "s.db")); await db.connect()
    cfg = Config("1:x", frozenset(), "", None, "INFO")
    sess = Fake()
    bot = Bot("1:" + "A" * 35, session=sess)
    bot._me = User(id=999, is_bot=True, first_name="bot", username="crymoder_bot")
    dp = Dispatcher(db=db, config=cfg)
    dp.include_routers(common.router, moderation.router, settings.router, captcha.router, automod.router)

    async def feed(d):
        await dp.feed_update(bot, Update.model_validate(d, context={"bot": bot}))

    # 1. /warn x3 -> на 3-й мут
    for _ in range(3):
        await feed(upd(ADMIN, "/warn спам", reply_to=USER))
    assert await db.warn_count(CHAT, USER) == 0, "после лимита варны сброшены"
    assert "RestrictChatMember" in sess.names(), sess.names()

    # 2. не-админ не может warn
    sess.calls.clear()
    await feed(upd(USER, "/warn x", reply_to=ADMIN))
    assert "RestrictChatMember" not in sess.names() and "SendMessage" not in sess.names()

    # 3. нельзя наказать админа
    sess.calls.clear()
    await feed(upd(ADMIN, "/ban", reply_to=ADMIN))
    assert "BanChatMember" not in sess.names()

    # 4. /mute 10m + /ban по ID
    sess.calls.clear()
    await feed(upd(ADMIN, "/mute 10m тест", reply_to=USER))
    r = [c for c in sess.calls if isinstance(c, RestrictChatMember)][0]
    assert r.permissions.can_send_messages is False and r.until_date is not None
    await feed(upd(ADMIN, "/ban 777"))
    assert any(isinstance(c, BanChatMember) and c.user_id == 777 for c in sess.calls)

    # 5. фильтр слов + автомодерация (удаление + варн)
    await feed(upd(ADMIN, "/addword казино"))
    sess.calls.clear()
    await feed(upd(USER, "Лучшее КАЗИНО здесь"))
    assert "DeleteMessage" in sess.names() and await db.warn_count(CHAT, USER) == 1, sess.names()

    # 6. инвайт-ссылка
    sess.calls.clear()
    await feed(upd(USER, "вступай t.me/+AbCdEfGh"))
    assert "DeleteMessage" in sess.names()

    # 7. флуд
    await db.set_setting(CHAT, "flood_limit", 3)
    sess.calls.clear()
    for i in range(4):
        await feed(upd(NEWBIE + 10, f"м{i}"))
    assert any(isinstance(c, RestrictChatMember) for c in sess.calls), sess.names()

    # 8. админ не триггерит автомод
    sess.calls.clear()
    for i in range(10):
        await feed(upd(ADMIN, f"a{i} t.me/+AbCdEfGh казино"))
    assert not any(isinstance(c, (DeleteMessage, RestrictChatMember)) for c in sess.calls)

    # 9. /settings + переключение кнопки
    sess.calls.clear()
    await feed(upd(ADMIN, "/settings"))
    assert "SendMessage" in sess.names()
    sent = [c for c in sess.calls if isinstance(c, SendMessage)][0]
    assert sent.reply_markup and len(sent.reply_markup.inline_keyboard) >= 10
    cb = {"update_id": 5000, "callback_query": {
        "id": "1", "chat_instance": "c", "data": "set:t:antilink",
        "from": {"id": ADMIN, "is_bot": False, "first_name": "a"},
        "message": {"message_id": 5, "date": int(time.time()),
                    "chat": {"id": CHAT, "type": "supergroup", "title": "T"}, "text": "s"}}}
    before = (await db.get_settings(CHAT))["antilink"]
    await feed(cb)
    assert (await db.get_settings(CHAT))["antilink"] == 1 - before

    # 10. капча: вход -> мут + кнопка; нажатие чужим -> отказ; нажатие своим -> размут
    join = {"update_id": 6000, "chat_member": {
        "chat": {"id": CHAT, "type": "supergroup", "title": "T"},
        "from": {"id": NEWBIE, "is_bot": False, "first_name": "n"}, "date": int(time.time()),
        "old_chat_member": {"status": "left", "user": {"id": NEWBIE, "is_bot": False, "first_name": "n"}},
        "new_chat_member": {"status": "member", "user": {"id": NEWBIE, "is_bot": False, "first_name": "n"}}}}
    sess.calls.clear()
    await feed(join)
    assert "RestrictChatMember" in sess.names() and "SendMessage" in sess.names(), sess.names()
    assert await db.get_captcha(CHAT, NEWBIE)
    cb2 = {"update_id": 6001, "callback_query": {
        "id": "2", "chat_instance": "c", "data": f"cap:{NEWBIE}",
        "from": {"id": USER, "is_bot": False, "first_name": "x"},
        "message": {"message_id": 9, "date": int(time.time()),
                    "chat": {"id": CHAT, "type": "supergroup", "title": "T"}, "text": "c"}}}
    await feed(cb2)
    assert await db.get_captcha(CHAT, NEWBIE), "чужое нажатие не должно проходить капчу"
    cb2["callback_query"]["from"]["id"] = NEWBIE
    cb2["update_id"] = 6002
    sess.calls.clear()
    await feed(cb2)
    assert await db.get_captcha(CHAT, NEWBIE) is None
    assert any(isinstance(c, RestrictChatMember) and c.permissions.can_send_messages for c in sess.calls)

    # 11. таймаут капчи -> кик
    await db.set_setting(CHAT, "captcha_timeout", 30)
    sess.calls.clear()
    join["update_id"] = 6003
    await feed(join)
    row = await db.get_captcha(CHAT, NEWBIE)
    await captcha._expire(bot, db, CHAT, NEWBIE, row["message_id"], 0)
    assert any(isinstance(c, BanChatMember) for c in sess.calls) and any(isinstance(c, UnbanChatMember) for c in sess.calls)

    await db.close()




def test_full_scenario():
    asyncio.run(scenario())
