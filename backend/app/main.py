import asyncio
import hashlib
import json
import re
import logging
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import base64
import time

from . import auth, caddyconf, certs, config, db, resources
from .chats import StartError, hub, Client, manager, read_events, incident_public

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("webpanel")


@asynccontextmanager
async def lifespan(app):
    if not config.DATABASE_URL or len(config.SECRET) < 32:
        raise RuntimeError("Не заданы WEBPANEL_DATABASE_URL / WEBPANEL_SECRET")
    await db.connect()
    if not await db.fetchval("SELECT count(*) FROM users"):
        code = auth.ensure_setup_code()
        log.warning("Пользователей нет. Код первичной настройки: %s (файл %s)", code, config.SETUP_CODE_FILE)
    await asyncio.to_thread(certs.ensure_ca)
    await manager.startup()
    access_task = asyncio.create_task(access_loop())
    yield
    access_task.cancel()
    await manager.shutdown()
    await db.close()


async def access_loop():
    """Держит Caddy в нужном режиме: окно без сертификата открывается/закрывается по времени."""
    was_open = None
    while True:
        try:
            is_open = caddyconf.window_open()
            await asyncio.to_thread(caddyconf.sync)
            if was_open and not is_open:
                # окно закрылось: входы без сертификата больше недействительны
                rows = await db.fetch("DELETE FROM auth_sessions WHERE cert_fp IS NULL RETURNING token_hash")
                hub.kick_tokens({r["token_hash"] for r in rows})
                log.warning("Окно входа без сертификата закрыто, сессий без сертификата завершено: %d", len(rows))
            was_open = is_open
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("access loop: %s", e)
        await asyncio.sleep(5)


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    h = resp.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("Referrer-Policy", "no-referrer")
    h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    h.setdefault("Content-Security-Policy",
                 "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                 "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
    if request.url.path.startswith("/api/"):
        h.setdefault("Cache-Control", "no-store")
    return resp


@app.exception_handler(StartError)
async def start_error_handler(request, exc: StartError):
    return JSONResponse(status_code=409, content={"detail": exc.as_dict()})


# --------------------------------------------------------------------------- модели
class Creds(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)


class SetupReq(Creds):
    setup_code: str = Field(max_length=100)


class RegisterReq(Creds):
    invite: str = Field(max_length=100)


class PasswordReq(BaseModel):
    old_password: str = Field(max_length=256)
    new_password: str = Field(max_length=256)


class NewChat(BaseModel):
    project: str | None = Field(default=None, max_length=200)
    model: str | None = Field(default=None, max_length=50)


class Message(BaseModel):
    text: str = Field(min_length=1, max_length=100_000)


class ApprovalReq(BaseModel):
    allow: bool


class UserCreate(Creds):
    rights: list[str] = []
    max_sessions: int | None = Field(default=None, ge=0, le=20)
    allowed_projects: list[str] = []


class UserPatch(BaseModel):
    rights: list[str] | None = None
    max_sessions: int | None = Field(default=None, ge=0, le=20)
    allowed_projects: list[str] | None = None


class AdminPassword(BaseModel):
    password: str = Field(max_length=256)


class InviteReq(BaseModel):
    note: str | None = Field(default=None, max_length=100)
    hours: int = Field(default=24, ge=1, le=24 * 14)


class DeleteReq(BaseModel):
    reason: str | None = Field(default=None, max_length=300)


class SettingsReq(BaseModel):
    ram_reserve_mb: int | None = Field(default=None, ge=256, le=65536)
    ram_critical_mb: int | None = Field(default=None, ge=128, le=65536)
    session_estimate_mb: int | None = Field(default=None, ge=100, le=8192)
    default_max_sessions: int | None = Field(default=None, ge=0, le=20)
    approval_timeout_s: int | None = Field(default=None, ge=30, le=3600)


MODELS = {"opus", "sonnet", "haiku", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-2"}


def public_user(u) -> dict:
    return {"id": u["id"], "username": u["username"], "is_admin": u["is_admin"], "rights": list(u["rights"] or []),
            "max_sessions": u["max_sessions"], "allowed_projects": list(u["allowed_projects"] or []),
            "created_at": u["created_at"].isoformat(), "last_login": u["last_login"].isoformat() if u["last_login"] else None}


def list_projects() -> list[str]:
    try:
        return sorted(p.name for p in config.WORKSPACE.iterdir() if p.is_dir() and not p.name.startswith("."))
    except OSError:
        return []


def user_projects(user) -> list[str]:
    allp = list_projects()
    return allp if user["is_admin"] else [p for p in allp if p in (user["allowed_projects"] or [])]


def require_csrf(request: Request):
    if request.headers.get("x-requested-with") != "webpanel":
        raise HTTPException(403, "CSRF check failed")
    auth._check_origin(request.headers, request.headers.get("host"))


# --------------------------------------------------------------------------- аутентификация
@app.get("/api/auth/status")
async def auth_status(request: Request):
    setup = not await db.fetchval("SELECT count(*) FROM users")
    user, acc, access_error = None, None, None
    try:
        user, acc = await auth.resolve(request)
    except HTTPException as e:
        access_error = e.detail
    cert = None
    if acc and acc.cert is not None:
        cert = {"name": acc.cert["name"], "bound": acc.cert["user_id"] is not None,
                "expires_at": acc.cert["expires_at"].isoformat()}
    until = caddyconf.window_until()
    return {"setup_needed": setup, "user": public_user(user) if user else None,
            "rights_catalog": config.RIGHTS, "cert": cert, "access_error": access_error,
            "nocert_window_until": until if until > time.time() else None}


async def _login_response(user_id: int, request: Request, acc):
    token = await auth.create_session(user_id, request, acc)
    user = await db.fetchrow("SELECT * FROM users WHERE id = $1", user_id)
    resp = JSONResponse({"user": public_user(user)})
    auth.set_cookie(resp, token)
    return resp


@app.post("/api/auth/setup")
async def setup(req: SetupReq, request: Request):
    require_csrf(request)
    acc = await auth.access_of(request)
    ip = auth.client_ip(request)
    auth.check_rate(ip, "__setup__")
    if await db.fetchval("SELECT count(*) FROM users"):
        raise HTTPException(400, "Сервер уже настроен")
    if not auth.check_setup_code(req.setup_code):
        auth.register_fail(ip, "__setup__")
        log.warning("LOGIN_FAIL ip=%s user=__setup__", ip)
        raise HTTPException(403, "Неверный код настройки (см. /etc/webpanel/setup-code на сервере)")
    auth.validate_credentials(req.username, req.password)
    s = await db.get_settings()
    try:
        uid = await db.fetchval(
            "INSERT INTO users(username, password_hash, is_admin, max_sessions) VALUES($1,$2,true,$3) RETURNING id",
            req.username, auth.hash_password(req.password), max(s["default_max_sessions"], 2))
    except Exception:
        raise HTTPException(400, "Не удалось создать администратора")
    auth.drop_setup_code()
    return await _login_response(uid, request, acc)


@app.post("/api/auth/register")
async def register(req: RegisterReq, request: Request):
    require_csrf(request)
    acc = await auth.access_of(request)
    if acc.no_cert:
        raise HTTPException(403, {"code": "cert_required", "message": "Для регистрации нового устройства нужен его сертификат (выдаёт администратор)."})
    if acc.cert["user_id"] is not None:
        raise HTTPException(403, {"code": "cert_other_user", "message": "Этот сертификат уже привязан к другому аккаунту."})
    ip = auth.client_ip(request)
    auth.check_rate(ip, "__invite__")
    auth.validate_credentials(req.username, req.password)
    code_hash = auth.hash_token(req.invite.strip())
    async with db.pool.acquire() as conn, conn.transaction():
        inv = await conn.fetchrow("SELECT * FROM invites WHERE code_hash = $1 AND used_at IS NULL AND expires_at > now() FOR UPDATE",
                                  code_hash)
        if not inv:
            auth.register_fail(ip, "__invite__")
            log.warning("LOGIN_FAIL ip=%s user=__invite__", ip)
            raise HTTPException(403, "Код приглашения недействителен или истёк")
        if await conn.fetchval("SELECT 1 FROM users WHERE lower(username) = lower($1)", req.username):
            raise HTTPException(400, "Такой логин уже занят")
        s = await db.get_settings()
        uid = await conn.fetchval("INSERT INTO users(username, password_hash, max_sessions) VALUES($1,$2,$3) RETURNING id",
                                  req.username, auth.hash_password(req.password), s["default_max_sessions"])
        await conn.execute("UPDATE invites SET used_by = $2, used_at = now() WHERE code_hash = $1", code_hash, uid)
    return await _login_response(uid, request, acc)


@app.post("/api/auth/login")
async def login(req: Creds, request: Request):
    require_csrf(request)
    acc = await auth.access_of(request)
    ip = auth.client_ip(request)
    auth.check_rate(ip, req.username)
    user = await db.fetchrow("SELECT * FROM users WHERE lower(username) = lower($1)", req.username)
    if not auth.verify_password(user["password_hash"] if user else None, req.password):
        auth.register_fail(ip, req.username)
        log.warning("LOGIN_FAIL ip=%s user=%s", ip, req.username[:40])
        await asyncio.sleep(0.5)
        raise HTTPException(401, "Неверный логин или пароль")
    auth.check_user_access(user, acc)
    auth.clear_fails(req.username)
    if auth.ph.check_needs_rehash(user["password_hash"]):
        await db.execute("UPDATE users SET password_hash = $2 WHERE id = $1", user["id"], auth.hash_password(req.password))
    return await _login_response(user["id"], request, acc)


@app.post("/api/auth/logout")
async def logout(request: Request, response: Response, user=Depends(auth.current_user)):
    await db.execute("DELETE FROM auth_sessions WHERE token_hash = $1", user["token_hash"])
    response.delete_cookie(config.COOKIE_NAME, path="/")
    return {"ok": True}


@app.post("/api/me/password")
async def change_password(req: PasswordReq, user=Depends(auth.current_user)):
    if not auth.verify_password(user["password_hash"], req.old_password):
        raise HTTPException(403, "Неверный текущий пароль")
    auth.validate_credentials(user["username"], req.new_password)
    await db.execute("UPDATE users SET password_hash = $2 WHERE id = $1", user["id"], auth.hash_password(req.new_password))
    await db.execute("DELETE FROM auth_sessions WHERE user_id = $1 AND token_hash <> $2", user["id"], user["token_hash"])
    return {"ok": True}


@app.get("/api/me/sessions")
async def my_sessions(user=Depends(auth.current_user)):
    rows = await db.fetch("SELECT * FROM auth_sessions WHERE user_id = $1 AND expires_at > now() ORDER BY last_seen DESC", user["id"])
    return [{"id": r["token_hash"][:16], "current": r["token_hash"] == user["token_hash"], "user_agent": r["user_agent"],
             "ip": r["ip"], "created_at": r["created_at"].isoformat(), "last_seen": r["last_seen"].isoformat()} for r in rows]


@app.delete("/api/me/sessions/{sid}")
async def revoke_session(sid: str, user=Depends(auth.current_user)):
    if len(sid) != 16:
        raise HTTPException(400)
    await db.execute("DELETE FROM auth_sessions WHERE user_id = $1 AND left(token_hash, 16) = $2", user["id"], sid)
    return {"ok": True}


# --------------------------------------------------------------------------- чаты
async def own_chat(chat_id: str, user):
    try:
        cid = uuid.UUID(chat_id)
    except ValueError:
        raise HTTPException(404, "Диалог не найден")
    row = await db.fetchrow("SELECT * FROM chats WHERE id = $1 AND user_id = $2", cid, user["id"])
    if not row:
        raise HTTPException(404, "Диалог не найден")
    return row


@app.get("/api/projects")
async def projects(user=Depends(auth.current_user)):
    return user_projects(user)


@app.get("/api/chats")
async def chats_list(user=Depends(auth.current_user)):
    rows = await db.fetch("SELECT * FROM chats WHERE user_id = $1 ORDER BY last_activity DESC", user["id"])
    return [manager.public_chat(r) for r in rows]


@app.post("/api/chats")
async def chat_create(req: NewChat, user=Depends(auth.current_user)):
    if req.project and req.project not in user_projects(user):
        raise HTTPException(403, "Нет доступа к этому проекту")
    if req.model and req.model not in MODELS:
        raise HTTPException(400, "Неизвестная модель")
    await manager.check_can_start(user)
    cid = uuid.uuid4()
    await db.execute("INSERT INTO chats(id, user_id, project, model, claude_session_id) VALUES($1,$2,$3,$4,$5)",
                     cid, user["id"], req.project or None, req.model or None, uuid.uuid4())
    row = await db.fetchrow("SELECT * FROM chats WHERE id = $1", cid)
    try:
        await manager.start(row, user)
    except Exception:
        await manager.delete(cid)
        raise
    row = await db.fetchrow("SELECT * FROM chats WHERE id = $1", cid)
    return manager.public_chat(row)


@app.get("/api/chats/{chat_id}/events")
async def chat_events(chat_id: str, after: int = 0, user=Depends(auth.current_user)):
    row = await own_chat(chat_id, user)
    return {"chat": manager.public_chat(row), "events": await asyncio.to_thread(read_events, row["id"], after)}


@app.post("/api/chats/{chat_id}/messages")
async def chat_send(chat_id: str, msg: Message, user=Depends(auth.current_user)):
    row = await own_chat(chat_id, user)
    await manager.send(row, user, msg.text)
    return {"ok": True}


@app.post("/api/chats/{chat_id}/interrupt")
async def chat_interrupt(chat_id: str, user=Depends(auth.current_user)):
    row = await own_chat(chat_id, user)
    await manager.interrupt(row["id"])
    return {"ok": True}


@app.post("/api/chats/{chat_id}/suspend")
async def chat_suspend(chat_id: str, user=Depends(auth.current_user)):
    row = await own_chat(chat_id, user)
    await manager.suspend(row["id"], notice="Сессия выгружена на диск по вашей команде. Контекст сохранён.")
    return {"ok": True}


@app.post("/api/chats/{chat_id}/resume")
async def chat_resume(chat_id: str, user=Depends(auth.current_user)):
    row = await own_chat(chat_id, user)
    await manager.start(row, user)
    return {"ok": True}


@app.delete("/api/chats/{chat_id}")
async def chat_delete(chat_id: str, user=Depends(auth.current_user)):
    row = await own_chat(chat_id, user)
    await manager.delete(row["id"])
    return {"ok": True}


@app.post("/api/chats/{chat_id}/approvals/{aid}")
async def chat_approval(chat_id: str, aid: str, req: ApprovalReq, user=Depends(auth.current_user)):
    row = await own_chat(chat_id, user)
    if not manager.resolve_approval(str(row["id"]), aid, req.allow):
        raise HTTPException(404, "Запрос уже обработан или истёк")
    return {"ok": True}


@app.get("/api/incidents")
async def incidents(user=Depends(auth.current_user)):
    rows = await db.fetch("SELECT * FROM incidents WHERE user_id = $1 ORDER BY ts DESC LIMIT 100", user["id"])
    return [incident_public(r) for r in rows]


@app.post("/api/incidents/seen")
async def incidents_seen(user=Depends(auth.current_user)):
    await db.execute("UPDATE incidents SET seen = true WHERE user_id = $1", user["id"])
    return {"ok": True}


@app.get("/api/resources")
async def get_resources(user=Depends(auth.current_user)):
    data = await asyncio.to_thread(resources.snapshot, bool(user["is_admin"]))
    s = await db.get_settings()
    data["reserve_mb"], data["critical_mb"] = s["ram_reserve_mb"], s["ram_critical_mb"]
    data["estimate_mb"] = await manager.estimate_mb()
    mine = await db.fetch("SELECT * FROM chats WHERE user_id = $1 AND state = 'running'", user["id"])
    data["my_sessions"] = [manager.public_chat(r) for r in mine]
    data["my_limit"] = user["max_sessions"]
    data["sessions_total"] = len(manager.rt)
    return data


# --------------------------------------------------------------------------- админ
@app.get("/api/admin/users")
async def admin_users(admin=Depends(auth.admin_user)):
    rows = await db.fetch("SELECT * FROM users ORDER BY id")
    counts = {}
    for r in await db.fetch("SELECT user_id, count(*) n FROM chats GROUP BY user_id"):
        counts[r["user_id"]] = r["n"]
    running = {}
    for rt in manager.rt.values():
        running[rt.user_id] = running.get(rt.user_id, 0) + 1
    return [{**public_user(r), "chats": counts.get(r["id"], 0), "running": running.get(r["id"], 0)} for r in rows]


def _clean_rights(rights):
    bad = [r for r in rights if r not in config.RIGHTS]
    if bad:
        raise HTTPException(400, f"Неизвестные права: {bad}")
    return sorted(set(rights))


def _clean_projects(projects):
    allp = set(list_projects())
    return sorted(p for p in set(projects) if p in allp)


@app.post("/api/admin/users")
async def admin_create_user(req: UserCreate, admin=Depends(auth.admin_user)):
    auth.validate_credentials(req.username, req.password)
    if await db.fetchval("SELECT 1 FROM users WHERE lower(username) = lower($1)", req.username):
        raise HTTPException(400, "Такой логин уже занят")
    s = await db.get_settings()
    uid = await db.fetchval(
        "INSERT INTO users(username, password_hash, rights, max_sessions, allowed_projects) VALUES($1,$2,$3,$4,$5) RETURNING id",
        req.username, auth.hash_password(req.password), _clean_rights(req.rights),
        req.max_sessions if req.max_sessions is not None else s["default_max_sessions"], _clean_projects(req.allowed_projects))
    return public_user(await db.fetchrow("SELECT * FROM users WHERE id = $1", uid))


@app.patch("/api/admin/users/{uid}")
async def admin_patch_user(uid: int, req: UserPatch, admin=Depends(auth.admin_user)):
    u = await db.fetchrow("SELECT * FROM users WHERE id = $1", uid)
    if not u:
        raise HTTPException(404)
    if req.rights is not None and not u["is_admin"]:
        await db.execute("UPDATE users SET rights = $2 WHERE id = $1", uid, _clean_rights(req.rights))
    if req.max_sessions is not None:
        await db.execute("UPDATE users SET max_sessions = $2 WHERE id = $1", uid, req.max_sessions)
    if req.allowed_projects is not None and not u["is_admin"]:
        await db.execute("UPDATE users SET allowed_projects = $2 WHERE id = $1", uid, _clean_projects(req.allowed_projects))
    return public_user(await db.fetchrow("SELECT * FROM users WHERE id = $1", uid))


@app.post("/api/admin/users/{uid}/password")
async def admin_set_password(uid: int, req: AdminPassword, admin=Depends(auth.admin_user)):
    u = await db.fetchrow("SELECT * FROM users WHERE id = $1", uid)
    if not u:
        raise HTTPException(404)
    auth.validate_credentials(u["username"], req.password)
    await db.execute("UPDATE users SET password_hash = $2 WHERE id = $1", uid, auth.hash_password(req.password))
    if uid != admin["id"]:
        await db.execute("DELETE FROM auth_sessions WHERE user_id = $1", uid)
        hub.kick_user(uid)
    return {"ok": True}


@app.post("/api/admin/users/{uid}/logout")
async def admin_logout_user(uid: int, admin=Depends(auth.admin_user)):
    if uid == admin["id"]:
        raise HTTPException(400, "Используйте «Устройства» в настройках")
    await db.execute("DELETE FROM auth_sessions WHERE user_id = $1", uid)
    hub.kick_user(uid)
    return {"ok": True}


@app.delete("/api/admin/users/{uid}")
async def admin_delete_user(uid: int, admin=Depends(auth.admin_user)):
    u = await db.fetchrow("SELECT * FROM users WHERE id = $1", uid)
    if not u:
        raise HTTPException(404)
    if u["is_admin"]:
        raise HTTPException(400, "Администратора удалить нельзя")
    for r in await db.fetch("SELECT id FROM chats WHERE user_id = $1", uid):
        await manager.delete(r["id"])
    hub.kick_user(uid)
    await db.execute("UPDATE client_certs SET revoked_at = now() WHERE user_id = $1 AND revoked_at IS NULL", uid)
    await db.execute("DELETE FROM users WHERE id = $1", uid)
    return {"ok": True}


@app.get("/api/admin/invites")
async def admin_invites(admin=Depends(auth.admin_user)):
    rows = await db.fetch("""SELECT i.*, u.username used_by_name FROM invites i LEFT JOIN users u ON u.id = i.used_by
                             ORDER BY created_at DESC LIMIT 50""")
    return [{"id": r["code_hash"][:16], "note": r["note"], "created_at": r["created_at"].isoformat(),
             "expires_at": r["expires_at"].isoformat(), "used_by": r["used_by_name"],
             "used_at": r["used_at"].isoformat() if r["used_at"] else None} for r in rows]


@app.post("/api/admin/invites")
async def admin_create_invite(req: InviteReq, admin=Depends(auth.admin_user)):
    code = "-".join(secrets.token_hex(3) for _ in range(3))
    await db.execute("INSERT INTO invites(code_hash, created_by, note, expires_at) VALUES($1,$2,$3,$4)",
                     auth.hash_token(code), admin["id"], req.note,
                     datetime.now(timezone.utc) + timedelta(hours=req.hours))
    return {"code": code}  # показывается один раз, в БД хранится только хэш


@app.delete("/api/admin/invites/{iid}")
async def admin_delete_invite(iid: str, admin=Depends(auth.admin_user)):
    await db.execute("DELETE FROM invites WHERE left(code_hash, 16) = $1", iid)
    return {"ok": True}


@app.get("/api/admin/sessions")
async def admin_sessions(admin=Depends(auth.admin_user)):
    rows = await db.fetch("SELECT c.*, u.username FROM chats c JOIN users u ON u.id = c.user_id "
                          "WHERE c.state = 'running' OR c.id = ANY($1) ORDER BY c.last_activity DESC",
                          [uuid.UUID(k) for k in manager.rt])
    return [{**manager.public_chat(r), "username": r["username"], "user_id": r["user_id"], "approvals": []} for r in rows]


async def _any_chat(chat_id: str):
    try:
        row = await db.fetchrow("SELECT * FROM chats WHERE id = $1", uuid.UUID(chat_id))
    except ValueError:
        row = None
    if not row:
        raise HTTPException(404)
    return row


@app.post("/api/admin/chats/{chat_id}/suspend")
async def admin_suspend(chat_id: str, admin=Depends(auth.admin_user)):
    row = await _any_chat(chat_id)
    own = row["user_id"] == admin["id"]
    await manager.suspend(row["id"], kind="manual" if own else "admin_suspend",
                          notice="Сессия выгружена на диск " + ("по вашей команде." if own else "администратором.") + " Контекст сохранён.")
    return {"ok": True}


@app.post("/api/admin/chats/{chat_id}/kill")
async def admin_kill(chat_id: str, admin=Depends(auth.admin_user)):
    row = await _any_chat(chat_id)
    await manager.emergency_kill(row["id"], kind="admin_kill", notice=(
        "Сессия экстренно завершена администратором. Текущий ответ прерван, история и контекст сохранены."))
    return {"ok": True}


@app.delete("/api/admin/chats/{chat_id}")
async def admin_delete_chat(chat_id: str, req: DeleteReq | None = None, admin=Depends(auth.admin_user)):
    row = await _any_chat(chat_id)
    own = row["user_id"] == admin["id"]
    await manager.delete(row["id"], by_admin=None if own else admin["username"], reason=req.reason if req else None)
    return {"ok": True}


@app.get("/api/admin/settings")
async def admin_get_settings(admin=Depends(auth.admin_user)):
    return await db.get_settings()


@app.put("/api/admin/settings")
async def admin_put_settings(req: SettingsReq, admin=Depends(auth.admin_user)):
    vals = {k: v for k, v in req.model_dump().items() if v is not None}
    cur = {**await db.get_settings(), **vals}
    if cur["ram_critical_mb"] >= cur["ram_reserve_mb"]:
        raise HTTPException(400, "Критический порог должен быть меньше резерва")
    await db.set_settings(vals)
    return await db.get_settings()


@app.get("/api/admin/incidents")
async def admin_incidents(admin=Depends(auth.admin_user)):
    rows = await db.fetch("SELECT i.*, u.username FROM incidents i LEFT JOIN users u ON u.id = i.user_id ORDER BY ts DESC LIMIT 200")
    return [{**incident_public(r), "username": r["username"]} for r in rows]


# --------------------------------------------------------------------------- сертификаты устройств
class CertReq(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    user_id: int | None = None
    days: int = Field(default=365, ge=1, le=3650)


class WindowReq(BaseModel):
    minutes: int = Field(ge=0, le=caddyconf.MAX_WINDOW_MIN)


def public_cert(r) -> dict:
    return {"id": r["id"], "name": r["name"], "fingerprint": r["fingerprint"], "user_id": r["user_id"],
            "username": r.get("username") if hasattr(r, "get") else None,
            "created_at": r["created_at"].isoformat(), "expires_at": r["expires_at"].isoformat(),
            "revoked_at": r["revoked_at"].isoformat() if r["revoked_at"] else None,
            "last_used": r["last_used"].isoformat() if r["last_used"] else None, "last_ip": r["last_ip"]}


@app.get("/api/admin/certs")
async def admin_certs(request: Request, admin=Depends(auth.admin_user)):
    rows = await db.fetch("SELECT c.*, u.username FROM client_certs c LEFT JOIN users u ON u.id = c.user_id "
                          "ORDER BY c.revoked_at IS NOT NULL, c.created_at DESC")
    acc = request.state.access
    return {"certs": [public_cert(r) for r in rows], "current_fp": acc.fp}


@app.post("/api/admin/certs")
async def admin_issue_cert(req: CertReq, admin=Depends(auth.admin_user)):
    if req.user_id is not None and not await db.fetchval("SELECT 1 FROM users WHERE id = $1", req.user_id):
        raise HTTPException(404, "Пользователь не найден")
    name = re.sub(r"[^\w .@-]", "", req.name, flags=re.UNICODE).strip() or "device"
    c = await asyncio.to_thread(certs.issue, name, req.days)
    row = await db.fetchrow(
        "INSERT INTO client_certs(name, fingerprint, serial, user_id, created_by, expires_at) VALUES($1,$2,$3,$4,$5,$6) RETURNING *",
        name, c["fingerprint"], c["serial"], req.user_id, admin["id"], c["not_after"])
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", name)[:40] or "device"
    # ключ устройства не сохраняется: файл и пароль отдаются один раз
    return {"cert": public_cert(row), "p12_b64": base64.b64encode(c["p12"]).decode(), "password": c["password"],
            "filename": f"webpanel-{safe}.p12"}


@app.post("/api/admin/certs/{cid}/revoke")
async def admin_revoke_cert(cid: int, request: Request, admin=Depends(auth.admin_user)):
    row = await db.fetchrow("SELECT * FROM client_certs WHERE id = $1", cid)
    if not row:
        raise HTTPException(404)
    if row["fingerprint"] == request.state.access.fp:
        raise HTTPException(400, "Нельзя отозвать сертификат устройства, с которого вы сейчас работаете")
    await db.execute("UPDATE client_certs SET revoked_at = now() WHERE id = $1 AND revoked_at IS NULL", cid)
    rows = await db.fetch("DELETE FROM auth_sessions WHERE cert_fp = $1 RETURNING token_hash", row["fingerprint"])
    hub.kick_tokens({r["token_hash"] for r in rows})
    return {"ok": True}


@app.get("/api/admin/access")
async def admin_access(admin=Depends(auth.admin_user)):
    until = caddyconf.window_until()
    return {"window_until": until if until > time.time() else None, "mode": caddyconf.applied_mode(),
            "configured": caddyconf.PARAMS.exists(), "max_minutes": caddyconf.MAX_WINDOW_MIN}


@app.post("/api/admin/access")
async def admin_set_access(req: WindowReq, request: Request, admin=Depends(auth.admin_user)):
    # Открыть/продлить окно можно только с устройства с сертификатом — иначе «временно» стало бы «всегда».
    if req.minutes > 0 and request.state.access.no_cert:
        raise HTTPException(403, "Продлить вход без сертификата можно только с устройства с сертификатом "
                                 "или с сервера: ./webpanel.sh open-window")
    caddyconf.set_window(req.minutes)
    try:
        await asyncio.to_thread(caddyconf.sync)
    except Exception as e:
        raise HTTPException(500, f"Caddy: {e}")
    return await admin_access(admin)


@app.get("/api/admin/server-ca")
async def admin_server_ca(admin=Depends(auth.admin_user)):
    data = certs.server_root_ca()
    if not data:
        raise HTTPException(404, "Корневой сертификат Caddy не найден")
    return Response(data, media_type="application/x-x509-ca-cert",
                    headers={"Content-Disposition": 'attachment; filename="webpanel-server-ca.crt"'})


# --------------------------------------------------------------------------- хук (только localhost)
@app.post("/internal/hook")
async def internal_hook(request: Request):
    # Прямые запросы с localhost без прокси. Через Caddy /internal/* не проксируется.
    if (request.client and request.client.host) != "127.0.0.1" or "x-forwarded-for" in request.headers:
        raise HTTPException(404)
    try:
        body = json.loads(await request.body())
    except Exception:
        raise HTTPException(400)
    return await manager.hook_decision(str(body.get("chat_id")), request.headers.get("x-hook-token", ""),
                                       body.get("tool_name"), body.get("tool_input"))


# --------------------------------------------------------------------------- WebSocket
@app.websocket("/api/ws")
async def ws_endpoint(ws: WebSocket):
    user = await auth.ws_user(ws)
    if not user:
        await ws.close(code=4401)
        return
    await ws.accept()
    client = Client(ws, user)
    hub.add(client)

    async def sender():
        while True:
            msg = await client.queue.get()
            await ws.send_text(json.dumps(msg, ensure_ascii=False, default=str))
            if msg.get("t") == "logout":
                await ws.close(code=4401)
                return

    send_task = asyncio.create_task(sender())
    try:
        while True:
            raw = await ws.receive_text()
            if len(raw) > 10_000:
                continue
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if msg.get("op") == "subscribe":
                chat_id = str(msg.get("chat_id"))
                try:
                    row = await own_chat(chat_id, user)
                except HTTPException:
                    client.push({"t": "error", "message": "Диалог не найден"})
                    continue
                client.subs = {chat_id}
                evs = await asyncio.to_thread(read_events, row["id"], int(msg.get("after") or 0))
                client.push({"t": "events", "chat_id": chat_id, "events": evs, "initial": True})
                client.push({"t": "chat", "chat": manager.public_chat(row)})
            elif msg.get("op") == "unsubscribe":
                client.subs = set()
            elif msg.get("op") == "ping":
                # заодно проверяем, что сессия входа ещё действительна
                if not await auth.ws_user(ws):
                    client.push({"t": "logout"})
                else:
                    client.push({"t": "pong"})
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        send_task.cancel()
        hub.remove(client)


# --------------------------------------------------------------------------- фронтенд (SPA)
if (config.FRONTEND_DIST / "assets").exists():
    app.mount("/assets", StaticFiles(directory=config.FRONTEND_DIST / "assets"), name="assets")


@app.get("/{path:path}", include_in_schema=False)
async def spa(path: str):
    if path.startswith(("api/", "internal/")):
        raise HTTPException(404)
    f = (config.FRONTEND_DIST / path).resolve()
    if path and f.is_file() and config.FRONTEND_DIST.resolve() in f.parents:
        return FileResponse(f)
    index = config.FRONTEND_DIST / "index.html"
    if index.exists():
        return FileResponse(index, headers={"Cache-Control": "no-cache"})
    return JSONResponse({"detail": "frontend не собран"}, status_code=503)
