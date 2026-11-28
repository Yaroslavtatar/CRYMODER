"""Админка сервера: свои чаты/БД/админы у каждого экземпляра, права владельца и администратора панели."""
import asyncio
from types import SimpleNamespace as NS

from fastapi.testclient import TestClient

from crymoder.config import Config
from crymoder.core import Core
from crymoder.db import DB
from crymoder.web.main import create_app

OWNER, ADMIN, MOD, USER = 1, 2, 3, 4
CHATS = {-1001: "Первый", -1002: "Второй"}


class FakeBot:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        async def f(*a, **kw):
            self.calls.append((name, a, kw))
            if name == "get_chat":
                return NS(permissions=None, type="supergroup")
            return True
        return f


def make(tmp_path):
    db = DB(str(tmp_path / "a.db"))
    cfg = Config("1:x", frozenset({OWNER}), str(tmp_path / "a.db"), None, "INFO",
                 public_url="http://panel.test", super_admins=frozenset({OWNER}))
    bot = FakeBot()
    core = Core(db, bot, cfg)
    core.me_id = 999
    for cid, title in CHATS.items():
        core.ensure_chat(cid, title)
        for u in (MOD, USER):
            db.execute("INSERT OR IGNORE INTO users(user_id,first_name,first_seen) VALUES(?,?,0)", (u, f"u{u}"))
            db.execute("INSERT INTO members(chat_id,user_id,first_seen,last_seen,msg_count,joined_at) VALUES(?,?,1,1,5,1)",
                       (cid, u))
    assert core.set_rank(-1001, MOD, 1, 0).ok
    return core, bot


def login(core, uid):
    tok = f"tok{uid}"
    core.db.execute("INSERT INTO web_tokens(token,user_id,expires_at) VALUES(?,?,9999999999)", (tok, uid))
    c = TestClient(create_app(core), follow_redirects=False)
    assert c.get(f"/login?t={tok}").status_code == 303
    return c


def test_access_levels(tmp_path):
    core, _ = make(tmp_path)
    core.db.execute("INSERT OR IGNORE INTO users(user_id,first_name,first_seen) VALUES(?,?,0)", (ADMIN, "adm"))
    assert core.add_panel_admin(ADMIN, OWNER).ok

    owner, adm, mod, user = (login(core, u) for u in (OWNER, ADMIN, MOD, USER))
    # владелец и админ панели видят админку, модератор и участник — нет
    assert owner.get("/api/me").json()["is_owner"] is True
    me = adm.get("/api/me").json()
    assert me["is_super"] and not me["is_owner"]
    assert owner.get("/api/admin/overview").status_code == 200
    assert adm.get("/api/admin/overview").status_code == 200
    assert mod.get("/api/admin/overview").status_code == 403
    assert user.get("/api/admin/chats").status_code == 401 or user.get("/api/admin/chats").status_code == 403
    # админ панели видит все чаты, но не управляет админами / не делает рассылки / не банит глобально
    assert len(adm.get("/api/admin/chats").json()) == 2
    for path, body in (("/api/admin/admins", {"user_id": 77}), ("/api/admin/broadcast", {"text": "x"}),
                       ("/api/admin/global_ban", {"user_id": USER}), ("/api/admin/chats/-1001/leave", {})):
        assert adm.post(path, json=body).status_code == 403, path
    assert adm.get("/api/admin/admins").json()["can_edit"] is False


def test_overview_chats_and_audit(tmp_path):
    core, _ = make(tmp_path)
    owner = login(core, OWNER)
    o = owner.get("/api/admin/overview").json()
    assert o["chats"] == 2 and o["members"] == 4 and o["staff"] == 1 and o["version"]
    chats = owner.get("/api/admin/chats").json()
    assert {c["title"] for c in chats} == {"Первый", "Второй"}
    assert owner.get("/api/admin/chats?q=перв").json()[0]["title"] == "Первый"
    assert any(r["action"] == "ранг" for r in owner.get("/api/admin/audit").json())
    assert owner.get("/api/admin/users?q=u4").json()[0]["user_id"] == USER
    assert owner.get("/api/admin/users?q=").json() == []


def test_panel_admins_management(tmp_path):
    core, _ = make(tmp_path)
    owner = login(core, OWNER)
    assert owner.post("/api/admin/admins", json={"user_id": ADMIN}).json()["ok"]
    assert ADMIN in core.super_admins and ADMIN not in core.owners
    assert not owner.post("/api/admin/admins", json={"user_id": ADMIN}).json()["ok"]       # дубль
    assert not owner.post("/api/admin/admins", json={"user_id": OWNER}).json()["ok"]       # владелец уже выше
    assert owner.post("/api/admin/admins", json={"user_id": "abc"}).status_code == 400
    assert owner.post("/api/admin/admins", json={"user_id": -5}).status_code == 400
    sess = login(core, ADMIN)
    assert sess.get("/api/admin/overview").status_code == 200
    assert owner.post("/api/admin/admins/remove", json={"user_id": ADMIN}).json()["ok"]
    assert ADMIN not in core.super_admins
    assert sess.get("/api/admin/overview").status_code == 401                              # сессия закрыта
    assert not owner.post("/api/admin/admins/remove", json={"user_id": OWNER}).json()["ok"]  # .env-владельца не убрать


def test_global_ban_leave_broadcast(tmp_path):
    core, bot = make(tmp_path)
    owner = login(core, OWNER)
    r = owner.post("/api/admin/global_ban", json={"user_id": USER, "ban": True, "reason": "спам"}).json()
    assert r["ok"] and "2" in r["text"]
    assert core.db.scalar("SELECT COUNT(*) FROM bans WHERE user_id=?", (USER,)) == 2
    assert not owner.post("/api/admin/global_ban", json={"user_id": OWNER}).json()["ok"]   # админа банить нельзя
    owner.post("/api/admin/global_ban", json={"user_id": USER, "ban": False})
    assert core.db.scalar("SELECT COUNT(*) FROM bans WHERE user_id=?", (USER,)) == 0

    bot.calls.clear()
    r = owner.post("/api/admin/broadcast", json={"text": "<b>привет</b>"}).json()
    sent = [c for c in bot.calls if c[0] == "send_message"]
    assert len(sent) == 2 and "&lt;b&gt;" in sent[0][1][1], "текст рассылки экранируется"
    assert owner.post("/api/admin/broadcast", json={"text": "  "}).status_code == 400

    assert owner.post("/api/admin/chats/-1002/leave", json={}).json()["ok"]
    assert any(c[0] == "leave_chat" for c in bot.calls)
    assert len(owner.get("/api/admin/chats").json()) == 1
    assert owner.get("/api/admin/chats?inactive=1").json()[0]["chat_id"] == -1002
    assert owner.post("/api/admin/chats/-1002/restore", json={}).json()["ok"]
    assert len(owner.get("/api/admin/chats").json()) == 2


def test_each_server_has_own_db_and_admins(tmp_path):
    """Два экземпляра бота не делят ни чаты, ни администраторов."""
    a, _ = make(tmp_path)
    b_dir = tmp_path / "y"
    b_dir.mkdir()
    b = Core(DB(str(b_dir / "b.db")), FakeBot(), Config("2:y", frozenset({50}), str(b_dir / "b.db"), None, "INFO",
                                                         super_admins=frozenset({50})))
    assert a.admin_overview()["chats"] == 2 and b.admin_overview()["chats"] == 0
    assert OWNER in a.super_admins and OWNER not in b.super_admins
    assert 50 in b.super_admins and 50 not in a.super_admins


def test_csrf_and_unauthenticated(tmp_path):
    core, _ = make(tmp_path)
    anon = TestClient(create_app(core), follow_redirects=False)
    assert anon.get("/api/admin/overview").status_code == 401
    owner = login(core, OWNER)
    assert owner.post("/api/admin/broadcast", content="text=1", headers={"content-type": "text/plain"}).status_code == 415
