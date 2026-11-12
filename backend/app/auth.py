"""Пароли, серверные сессии, ограничение перебора, CSRF-проверки."""
import hashlib
import re
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError
from fastapi import HTTPException, Request, WebSocket

from . import config, db

ph = PasswordHasher()  # argon2id
_DUMMY_HASH = ph.hash("dummy-password-for-timing")

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
MIN_PASSWORD = 10


def hash_token(token: str) -> str:
    return hashlib.sha256((config.SECRET + token).encode()).hexdigest()


def validate_credentials(username: str, password: str):
    if not USERNAME_RE.match(username or ""):
        raise HTTPException(400, "Логин: 3–32 символа, латиница, цифры, _ . -")
    if len(password or "") < MIN_PASSWORD:
        raise HTTPException(400, f"Пароль должен быть не короче {MIN_PASSWORD} символов")
    if len(password) > 256:
        raise HTTPException(400, "Слишком длинный пароль")


def hash_password(p: str) -> str:
    return ph.hash(p)


def verify_password(hash_: str | None, p: str) -> bool:
    try:
        return ph.verify(hash_ or _DUMMY_HASH, p) and hash_ is not None
    except (VerificationError, InvalidHashError):
        return False


# ---- ограничение перебора паролей (в памяти) ----
_fails: dict[str, deque] = defaultdict(deque)
FAIL_WINDOW = 15 * 60
FAIL_LIMIT_USER = 5
FAIL_LIMIT_IP = 20


def _recent(key: str) -> deque:
    q = _fails[key]
    now = time.time()
    while q and q[0] < now - FAIL_WINDOW:
        q.popleft()
    return q


def check_rate(ip: str, username: str):
    if len(_recent("ip:" + ip)) >= FAIL_LIMIT_IP or len(_recent("u:" + username.lower())) >= FAIL_LIMIT_USER:
        raise HTTPException(429, "Слишком много неудачных попыток. Подождите 15 минут.")


def register_fail(ip: str, username: str):
    _recent("ip:" + ip).append(time.time())
    _recent("u:" + username.lower()).append(time.time())


def clear_fails(username: str):
    _fails.pop("u:" + username.lower(), None)


# ---- сертификаты устройств (mTLS) ----
class Access:
    """Чем подтверждено устройство: сертификатом или временным окном без сертификата."""

    def __init__(self, fp: str | None, cert, window: bool):
        self.fp, self.cert, self.window = fp, cert, window

    @property
    def no_cert(self) -> bool:
        return self.cert is None


_FP_RE = re.compile(r"^[0-9a-f]{64}$")


async def access_of(conn) -> Access:
    """Проверка устройства. Отпечаток сертификата выставляет Caddy (header_up перезаписывает значение клиента).

    Без сертификата пускаем, только пока открыто временное окно (и дальше — только админа)."""
    from . import caddyconf
    fp = (conn.headers.get("x-client-cert-fp") or "").strip().lower().replace(":", "")
    if fp and not _FP_RE.match(fp):
        fp = ""
    window = caddyconf.window_open()
    if not fp:
        if not window:
            raise HTTPException(403, {"code": "cert_required", "message": "Нужен сертификат устройства. Попросите администратора выпустить его."})
        return Access(None, None, True)
    cert = await db.fetchrow("SELECT * FROM client_certs WHERE fingerprint = $1", fp)
    if not cert or cert["revoked_at"] or cert["expires_at"] < datetime.now(timezone.utc):
        raise HTTPException(403, {"code": "cert_revoked", "message": "Сертификат этого устройства отозван, истёк или не зарегистрирован."})
    if not cert["last_used"] or (datetime.now(timezone.utc) - cert["last_used"]).total_seconds() > 300:
        ip = conn.client.host if conn.client else None
        await db.execute("UPDATE client_certs SET last_used = now(), last_ip = $2 WHERE id = $1", cert["id"], ip)
    return Access(fp, cert, window)


def check_user_access(user, acc: Access):
    """Кто может входить с этим устройством."""
    if acc.no_cert and not user["is_admin"]:
        raise HTTPException(403, {"code": "cert_required",
                                  "message": "Без сертификата устройства вход разрешён только администратору (и только временно)."})
    if acc.cert is not None and acc.cert["user_id"] is not None and acc.cert["user_id"] != user["id"]:
        raise HTTPException(403, {"code": "cert_other_user", "message": "Этот сертификат выдан другому пользователю."})


async def bind_cert(acc: Access, user_id: int):
    if acc.cert is not None and acc.cert["user_id"] is None:
        await db.execute("UPDATE client_certs SET user_id = $2 WHERE id = $1 AND user_id IS NULL", acc.cert["id"], user_id)


# ---- сессии ----
async def create_session(user_id: int, request: Request, acc: Access) -> str:
    token = secrets.token_urlsafe(32)
    await db.execute(
        "INSERT INTO auth_sessions(token_hash, user_id, expires_at, user_agent, ip, cert_fp) VALUES($1,$2,$3,$4,$5,$6)",
        hash_token(token), user_id,
        datetime.now(timezone.utc) + timedelta(days=config.SESSION_TTL_DAYS if acc.cert is not None else 1),
        (request.headers.get("user-agent") or "")[:300], client_ip(request), acc.fp,
    )
    await bind_cert(acc, user_id)
    await db.execute("UPDATE users SET last_login = now() WHERE id = $1", user_id)
    return token


def set_cookie(response, token: str):
    response.set_cookie(
        config.COOKIE_NAME, token, max_age=config.SESSION_TTL_DAYS * 86400,
        httponly=True, secure=True, samesite="strict", path="/",
    )


async def _session_user(token: str | None):
    if not token or len(token) > 100:
        return None
    th = hash_token(token)
    row = await db.fetchrow(
        """SELECT u.*, s.token_hash, s.last_seen, s.cert_fp FROM auth_sessions s JOIN users u ON u.id = s.user_id
           WHERE s.token_hash = $1 AND s.expires_at > now()""", th)
    if row and (datetime.now(timezone.utc) - row["last_seen"]).total_seconds() > 300:
        await db.execute("UPDATE auth_sessions SET last_seen = now() WHERE token_hash = $1", th)
    return row


async def resolve(conn) -> tuple[object | None, Access]:
    """Пользователь по cookie + проверка устройства. Бросает 403, если устройство не допущено."""
    acc = await access_of(conn)
    user = await _session_user(conn.cookies.get(config.COOKIE_NAME))
    if not user:
        return None, acc
    if user["cert_fp"] != acc.fp:
        if user["cert_fp"] is None and acc.cert is not None:
            # вход был без сертификата (окно), теперь устройство предъявило сертификат — закрепляем
            check_user_access(user, acc)
            await db.execute("UPDATE auth_sessions SET cert_fp = $2, expires_at = $3 WHERE token_hash = $1",
                             user["token_hash"], acc.fp, datetime.now(timezone.utc) + timedelta(days=config.SESSION_TTL_DAYS))
            await bind_cert(acc, user["id"])
            return await _session_user(conn.cookies.get(config.COOKIE_NAME)), acc
        return None, acc  # cookie с другого устройства — не принимаем
    check_user_access(user, acc)
    return user, acc


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _check_origin(headers, host: str | None):
    origin = headers.get("origin")
    if origin:
        o = re.sub(r"^https?://", "", origin).rstrip("/")
        if o != host:
            raise HTTPException(403, "Неверный Origin")


async def current_user(request: Request):
    """Зависимость FastAPI: требует вход. Для изменяющих запросов — защита от CSRF."""
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        # SameSite=Strict + обязательный заголовок (браузер не пошлёт его кросс-доменно без CORS) + Origin
        if request.headers.get("x-requested-with") != "webpanel":
            raise HTTPException(403, "CSRF check failed")
        _check_origin(request.headers, request.headers.get("host"))
    user, acc = await resolve(request)
    if not user:
        raise HTTPException(401, "Требуется вход")
    request.state.access = acc
    return user


async def admin_user(request: Request):
    user = await current_user(request)
    if not user["is_admin"]:
        raise HTTPException(403, "Только для администратора")
    return user


async def ws_user(ws: WebSocket):
    try:
        _check_origin(ws.headers, ws.headers.get("host"))
        user, _ = await resolve(ws)
        return user
    except HTTPException:
        return None


# ---- первичная настройка: код для регистрации первого (админского) пользователя ----
def ensure_setup_code() -> str:
    f = config.SETUP_CODE_FILE
    if f.exists():
        return f.read_text().strip()
    code = secrets.token_urlsafe(12)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(code + "\n")
    f.chmod(0o600)
    return code


def check_setup_code(code: str) -> bool:
    f = config.SETUP_CODE_FILE
    return f.exists() and secrets.compare_digest(f.read_text().strip(), (code or "").strip())


def drop_setup_code():
    config.SETUP_CODE_FILE.unlink(missing_ok=True)
