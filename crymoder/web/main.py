"""Веб-панель: вход по одноразовой ссылке из бота, JSON API и статическая страница."""
import secrets
from pathlib import Path

from fastapi import Body, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from .. import commands, registry  # commands: чтобы заполнить реестр команд
from ..core import DISABLED, SETTINGS, Core
from ..utils import now, take_period

STATIC = Path(__file__).parent / "static"
SESSION_TTL = 7 * 86400

# действие панели -> код команды для проверки прав (те же права, что и в чате)
ACTION_CODE = {"warn": "варн", "unwarn": "снять варн", "clear_warns": "снять варн", "mute": "мут",
               "unmute": "снять мут", "ban": "бан", "unban": "разбан", "kick": "кик", "rank": "модер"}
CLEAN_CODE = {"inactive": "кик неактив", "silent": "кик молчунов", "newcomers": "кик новичков",
              "deleted": "кик удалённых", "active": "кик актив"}


def create_app(core: Core) -> FastAPI:
    app = FastAPI(title="CRYMODER", docs_url=None, redoc_url=None, openapi_url=None)

    # ---------- авторизация ----------
    def current_user(request: Request) -> int:
        sid = request.cookies.get("sid")
        r = core.db.one("SELECT user_id FROM web_sessions WHERE sid=? AND expires_at>?", (sid, now())) if sid else None
        if not r:
            raise HTTPException(401, "Нужен вход: напишите боту «панель»")
        return r["user_id"]

    def is_super(uid: int) -> bool:
        return uid in core.super_admins

    def chat_rank(chat_id: int, uid: int) -> int:
        return 5 if is_super(uid) else core.rank_of(chat_id, uid)

    def require_chat(chat_id: int, uid: int) -> None:
        if not core.db.one("SELECT 1 FROM chats WHERE chat_id=? AND active=1", (chat_id,)):
            raise HTTPException(404, "Чат не найден")
        if chat_rank(chat_id, uid) < 1:
            raise HTTPException(403, "Нет доступа к этому чату")

    def require_access(chat_id: int, uid: int, code: str) -> None:
        require_chat(chat_id, uid)
        if not is_super(uid) and not core.has_access(chat_id, uid, code):
            raise HTTPException(403, f"Недостаточно прав: «{registry.COMMANDS[code].title}»")

    @app.get("/login")
    async def login(t: str = ""):
        r = core.db.one("SELECT user_id FROM web_tokens WHERE token=? AND expires_at>?", (t, now()))
        if not r:
            return JSONResponse({"error": "Ссылка недействительна или устарела. Напишите боту «панель» ещё раз."},
                                status_code=400)
        core.db.execute("DELETE FROM web_tokens WHERE token=?", (t,))
        sid = secrets.token_urlsafe(32)
        core.db.execute("INSERT INTO web_sessions(sid,user_id,expires_at) VALUES(?,?,?)",
                        (sid, r["user_id"], now() + SESSION_TTL))
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie("sid", sid, max_age=SESSION_TTL, httponly=True, samesite="lax",
                        secure=core.config.public_url.startswith("https://") if core.config else False)
        return resp

    @app.post("/api/logout")
    async def logout(request: Request):
        sid = request.cookies.get("sid")
        if sid:
            core.db.execute("DELETE FROM web_sessions WHERE sid=?", (sid,))
        resp = JSONResponse({"ok": True})
        resp.delete_cookie("sid")
        return resp

    # CSRF: изменяющие запросы принимаем только как JSON (браузер не шлёт кросс-сайтовый JSON без CORS)
    @app.middleware("http")
    async def csrf_guard(request: Request, call_next):
        if request.method == "POST" and request.url.path != "/api/logout":
            if "application/json" not in request.headers.get("content-type", ""):
                return JSONResponse({"detail": "Ожидается JSON"}, status_code=415)
        return await call_next(request)

    # ---------- чтение ----------
    @app.get("/api/me")
    async def me(uid: int = Depends(current_user)):
        if is_super(uid):
            rows = core.db.query("SELECT chat_id FROM chats WHERE active=1 ORDER BY title")
        else:
            rows = core.db.query("SELECT c.chat_id FROM chats c JOIN ranks r ON r.chat_id=c.chat_id"
                                 " WHERE r.user_id=? AND r.rank>=1 AND c.active=1 ORDER BY c.title", (uid,))
        chats = [{"chat_id": r["chat_id"], "title": core.db.scalar("SELECT title FROM chats WHERE chat_id=?",
                                                                   (r["chat_id"],)),
                  "rank": chat_rank(r["chat_id"], uid)} for r in rows]
        return {"user_id": uid, "name": core.name(uid), "chats": chats}

    @app.get("/api/chats/{chat_id}/overview")
    async def overview(chat_id: int, uid: int = Depends(current_user)):
        require_chat(chat_id, uid)
        o = core.chat_overview(chat_id)
        o["my_rank"] = chat_rank(chat_id, uid)
        o["chat_open"] = core.get_raw(chat_id, "chat_open") != "0"
        return o

    @app.get("/api/chats/{chat_id}/members")
    async def members(chat_id: int, q: str = "", page: int = 0, uid: int = Depends(current_user)):
        require_chat(chat_id, uid)
        return core.members_list(chat_id, q.strip()[:64], 50, max(page, 0) * 50)

    @app.get("/api/chats/{chat_id}/punishments")
    async def punishments(chat_id: int, uid: int = Depends(current_user)):
        require_chat(chat_id, uid)
        return core.punishments(chat_id)

    @app.get("/api/chats/{chat_id}/staff")
    async def staff(chat_id: int, uid: int = Depends(current_user)):
        require_chat(chat_id, uid)
        return [{"user_id": r["user_id"], "name": core.name(r["user_id"]), "rank": r["rank"],
                 "by": core.name(r["appointed_by"]) if r["appointed_by"] else "система", "at": r["at"]}
                for r in core.staff(chat_id)]

    @app.get("/api/chats/{chat_id}/audit")
    async def audit(chat_id: int, uid: int = Depends(current_user)):
        require_chat(chat_id, uid)
        rows = core.db.query("SELECT * FROM audit WHERE chat_id=? ORDER BY id DESC LIMIT 200", (chat_id,))
        return [{"at": r["at"], "by": core.name(r["by_id"]) if r["by_id"] else "система", "action": r["action"],
                 "detail": r["detail"], "source": r["source"]} for r in rows]

    @app.get("/api/chats/{chat_id}/settings")
    async def get_settings(chat_id: int, uid: int = Depends(current_user)):
        require_chat(chat_id, uid)
        return core.all_settings(chat_id)

    @app.get("/api/chats/{chat_id}/access")
    async def get_access(chat_id: int, uid: int = Depends(current_user)):
        require_chat(chat_id, uid)
        return [{"code": c.code, "title": c.title, "section": c.section, "number": c.number,
                 "default": c.default_rank, "rank": core.required_rank(chat_id, c.code)}
                for c in registry.COMMANDS.values() if c.phrases]

    # ---------- изменения ----------
    @app.post("/api/chats/{chat_id}/action")
    async def action(chat_id: int, body: dict = Body(...), uid: int = Depends(current_user)):
        act = str(body.get("action", ""))
        if act not in ACTION_CODE:
            raise HTTPException(400, "Неизвестное действие")
        code = ACTION_CODE[act]
        if act == "rank" and int(body.get("rank", 1)) == 0:
            code = "снять"
        require_access(chat_id, uid, code)
        try:
            target = int(body.get("user_id"))
        except (TypeError, ValueError):
            raise HTTPException(400, "Не указан пользователь")
        reason = str(body.get("reason", ""))[:300]
        period = str(body.get("period", "")).strip()
        secs = None
        if period:
            secs, rest = take_period(period.split())
            if secs is None or rest:
                raise HTTPException(400, "Не понял срок. Примеры: «2 часа», «3 дня», «навсегда»")
        by = uid
        if act == "warn":
            r = await core.warn(chat_id, target, by, secs, reason, "web")
        elif act == "unwarn":
            r = core.unwarn(chat_id, target, by, source="web")
        elif act == "clear_warns":
            r = core.unwarn(chat_id, target, by, all_=True, source="web")
        elif act == "mute":
            r = await core.mute(chat_id, target, by, secs, reason, "web")
        elif act == "unmute":
            r = await core.unmute(chat_id, target, by, "web")
        elif act == "ban":
            r = await core.ban(chat_id, target, by, secs, reason, "web")
        elif act == "unban":
            r = await core.unban(chat_id, target, by, "web")
        elif act == "kick":
            r = await core.kick(chat_id, target, by, reason, "web")
        else:
            try:
                new = int(body.get("rank"))
            except (TypeError, ValueError):
                raise HTTPException(400, "Не указан ранг")
            r = core.set_rank(chat_id, target, new, by, "web")
        return {"ok": r.ok, "text": _plain(r.text)}

    @app.post("/api/chats/{chat_id}/amnesty")
    async def amnesty(chat_id: int, uid: int = Depends(current_user)):
        require_access(chat_id, uid, "амнистия")
        r = await core.amnesty(chat_id, uid, "web")
        return {"ok": r.ok, "text": _plain(r.text)}

    @app.post("/api/chats/{chat_id}/chat_open")
    async def chat_open(chat_id: int, body: dict = Body(...), uid: int = Depends(current_user)):
        require_access(chat_id, uid, "чат вкл выкл")
        r = await core.set_chat_open(chat_id, bool(body.get("open")), uid, "web")
        return {"ok": r.ok, "text": _plain(r.text)}

    @app.post("/api/chats/{chat_id}/settings")
    async def post_settings(chat_id: int, body: dict = Body(...), uid: int = Depends(current_user)):
        require_access(chat_id, uid, "безопасность")
        key = str(body.get("key", ""))
        if key not in SETTINGS:
            raise HTTPException(400, "Неизвестная настройка")
        r = core.set_setting(chat_id, key, body.get("value"), uid, "web")
        return {"ok": r.ok, "text": _plain(r.text)}

    @app.post("/api/chats/{chat_id}/access")
    async def post_access(chat_id: int, body: dict = Body(...), uid: int = Depends(current_user)):
        require_access(chat_id, uid, "дк")
        code = str(body.get("code", ""))
        if code not in registry.COMMANDS:
            raise HTTPException(400, "Неизвестная команда")
        try:
            rank = int(body.get("rank"))
        except (TypeError, ValueError):
            raise HTTPException(400, "Не указан ранг")
        if code in ("дк", "доступ команд") and rank >= DISABLED:
            raise HTTPException(400, "Команды управления доступом отключить нельзя")
        r = core.set_access(chat_id, code, rank, uid, "web")
        return {"ok": r.ok, "text": _plain(r.text)}

    @app.post("/api/chats/{chat_id}/access/reset")
    async def reset_access(chat_id: int, uid: int = Depends(current_user)):
        require_access(chat_id, uid, "сброс дк")
        return {"ok": True, "text": _plain(core.reset_access(chat_id, uid, None, "web").text)}

    def _clean_ids(chat_id: int, body: dict):
        mode = str(body.get("mode", ""))
        if mode not in CLEAN_CODE:
            raise HTTPException(400, "Неизвестный режим")
        try:
            val = int(body.get("value") or 0)
        except (TypeError, ValueError):
            raise HTTPException(400, "Значение должно быть числом")
        unit = {"minutes": 60, "hours": 3600, "days": 86400}.get(str(body.get("unit", "days")), 86400)
        if mode == "inactive":
            if body.get("by") == "count":
                return mode, core.inactive_ids(chat_id, count=max(val, 0))
            if val * unit < 86400:
                raise HTTPException(400, "Минимальный период — 24 часа")
            return mode, core.inactive_ids(chat_id, secs=val * unit)
        if mode == "silent":
            return mode, core.inactive_ids(chat_id, only_silent=True, period=val * unit or None)
        if mode == "newcomers":
            return mode, core.inactive_ids(chat_id, newcomers_secs=max(val * unit, 60))
        if mode == "active":
            return mode, core.inactive_ids(chat_id, active_secs=min(max(val * unit, 60), 600))
        return mode, commands._deleted_ids(core, chat_id)

    @app.post("/api/chats/{chat_id}/clean/preview")
    async def clean_preview(chat_id: int, body: dict = Body(...), uid: int = Depends(current_user)):
        mode = str(body.get("mode", ""))
        require_access(chat_id, uid, CLEAN_CODE.get(mode, "кик неактив"))
        _, ids = _clean_ids(chat_id, body)
        return {"count": len(ids), "sample": [core.name(i) for i in ids[:15]]}

    @app.post("/api/chats/{chat_id}/clean/run")
    async def clean_run(chat_id: int, body: dict = Body(...), uid: int = Depends(current_user)):
        mode = str(body.get("mode", ""))
        require_access(chat_id, uid, CLEAN_CODE.get(mode, "кик неактив"))
        _, ids = _clean_ids(chat_id, body)
        r = await core.kick_many(chat_id, ids, uid, "web")
        return {"ok": True, "text": _plain(r.text)}

    # ---------- страница ----------
    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})

    return app


def _plain(html_text: str) -> str:
    import re
    import html as _h
    return _h.unescape(re.sub(r"<[^>]+>", "", html_text))
