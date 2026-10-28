from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from fastapi import HTTPException, Request, Response
from fastapi.responses import JSONResponse

from cairn.server import db
from cairn.server.services import utcnow

SESSION_COOKIE = "cairn_session"
CSRF_COOKIE = "cairn_csrf"
SESSION_TTL = timedelta(hours=12)
PBKDF2_ITERATIONS = 600_000
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_MAX_ATTEMPTS = 5
MIN_NEW_PASSWORD_LENGTH = 8
PASSWORD_SOURCE_KEY = "password_source"
def credentials_file() -> Path:
    env = os.environ.get("CAIRN_CREDENTIALS_FILE", "").strip()
    if env:
        return Path(env)
    return Path.home() / ".local" / "share" / "cairn" / "credentials"

_PUBLIC_EXACT = {"/", "/healthz", "/auth/login", "/auth/logout", "/auth/me"}
_PUBLIC_PREFIXES = ("/static/",)
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

_login_attempts: dict[str, list[float]] = {}
_login_lock = threading.Lock()


@dataclass(frozen=True, slots=True)
class Identity:
    username: str
    via: str  # "session" | "token"
    csrf_token: str | None = None
    session_id: str | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    if salt is None:
        salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2$sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, algo, iterations_s, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    if scheme != "pbkdf2" or algo != "sha256":
        return False
    try:
        iterations = int(iterations_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except ValueError:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _chmod_private(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _write_credentials_file(username: str, password: str | None, api_token: str | None) -> Path:
    path = credentials_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            existing[key.strip()] = value.strip()
    if username:
        existing["username"] = username
    if password:
        existing["password"] = password
    if api_token:
        existing["api_token"] = api_token
    body = (
        "# Cairn local credentials. File mode 0600. Do not commit or share.\n"
        f"username={existing.get('username', username)}\n"
        f"password={existing.get('password', '')}\n"
        f"api_token={existing.get('api_token', '')}\n"
    )
    path.write_text(body, encoding="utf-8")
    _chmod_private(path)
    return path


def _password_reset_requested() -> bool:
    return os.environ.get("CAIRN_AUTH_PASSWORD_RESET", "").lower() in {"1", "true", "yes"}


def _meta_get(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM auth_meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _meta_set(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO auth_meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def bootstrap_auth() -> dict[str, str]:
    """Create the admin user and API token on first boot.

    CAIRN_AUTH_PASSWORD still seeds or rotates the password, except after a
    UI password change (`password_source=user`). Force a reset with
    CAIRN_AUTH_PASSWORD_RESET=1.
    """
    username = (os.environ.get("CAIRN_AUTH_USERNAME") or "admin").strip()
    env_password = os.environ.get("CAIRN_AUTH_PASSWORD")
    env_token = os.environ.get("CAIRN_API_TOKEN")
    generated: dict[str, str] = {}

    with db.get_conn() as conn:
        row = conn.execute("SELECT username, password_hash FROM auth_users WHERE username = ?", (username,)).fetchone()
        source = _meta_get(conn, PASSWORD_SOURCE_KEY)
        if env_password and (row is None or source != "user" or _password_reset_requested()):
            conn.execute(
                "INSERT INTO auth_users (username, password_hash, created_at) VALUES (?, ?, ?) "
                "ON CONFLICT(username) DO UPDATE SET password_hash = excluded.password_hash",
                (username, hash_password(env_password), utcnow()),
            )
            _meta_set(conn, PASSWORD_SOURCE_KEY, "env")
        elif row is None:
            password = secrets.token_urlsafe(18)
            conn.execute(
                "INSERT INTO auth_users (username, password_hash, created_at) VALUES (?, ?, ?)",
                (username, hash_password(password), utcnow()),
            )
            _meta_set(conn, PASSWORD_SOURCE_KEY, "generated")
            generated["password"] = password
        else:
            conn.execute(
                "INSERT OR IGNORE INTO auth_users (username, password_hash, created_at) VALUES (?, ?, ?)",
                (username, row["password_hash"], utcnow()),
            )

        token_row = conn.execute("SELECT value FROM auth_meta WHERE key = 'api_token_hash'").fetchone()
        if env_token:
            conn.execute(
                "INSERT INTO auth_meta (key, value) VALUES ('api_token_hash', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (hash_token(env_token),),
            )
        elif token_row is None:
            token = "cairn_" + secrets.token_urlsafe(32)
            conn.execute(
                "INSERT INTO auth_meta (key, value) VALUES ('api_token_hash', ?)",
                (hash_token(token),),
            )
            generated["api_token"] = token

        conn.execute("DELETE FROM auth_users WHERE username != ?", (username,))

    if generated:
        _write_credentials_file(username, generated.get("password"), generated.get("api_token"))
        generated["username"] = username
        generated["credentials_file"] = str(credentials_file())
    return generated


def _trust_proxy() -> bool:
    return os.environ.get("CAIRN_TRUST_PROXY", "").lower() in {"1", "true", "yes"}


def _client_ip(request: Request) -> str:
    if _trust_proxy():
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def reset_login_attempts() -> None:
    with _login_lock:
        _login_attempts.clear()


def check_login_rate_limit(request: Request) -> None:
    ip = _client_ip(request)
    now = time.monotonic()
    with _login_lock:
        stamps = [stamp for stamp in _login_attempts.get(ip, []) if now - stamp < LOGIN_WINDOW_SECONDS]
        if len(stamps) >= LOGIN_MAX_ATTEMPTS:
            _login_attempts[ip] = stamps
            raise HTTPException(429, "Too many login attempts. Try again later.")
        stamps.append(now)
        _login_attempts[ip] = stamps


def clear_login_failures(request: Request) -> None:
    ip = _client_ip(request)
    with _login_lock:
        _login_attempts.pop(ip, None)


def cookie_secure(request: Request) -> bool:
    if os.environ.get("CAIRN_COOKIE_SECURE", "").lower() in {"1", "true", "yes"}:
        return True
    return request.url.scheme == "https"


def _set_session_cookies(response: Response, request: Request, session_id: str, csrf_token: str) -> None:
    secure = cookie_secure(request)
    max_age = int(SESSION_TTL.total_seconds())
    response.set_cookie(
        SESSION_COOKIE,
        session_id,
        max_age=max_age,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf_token,
        max_age=max_age,
        httponly=False,
        secure=secure,
        samesite="lax",
        path="/",
    )


def create_session(username: str) -> tuple[str, str]:
    session_id = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    now = _now()
    expires = now + SESSION_TTL
    with db.get_conn() as conn:
        conn.execute("DELETE FROM auth_sessions WHERE expires_at < ?", (utcnow(),))
        conn.execute(
            "INSERT INTO auth_sessions (id, username, created_at, expires_at, csrf_token) VALUES (?, ?, ?, ?, ?)",
            (
                session_id,
                username,
                now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
                csrf_token,
            ),
        )
    return session_id, csrf_token


def destroy_session(session_id: str | None) -> None:
    if not session_id:
        return
    with db.get_conn() as conn:
        conn.execute("DELETE FROM auth_sessions WHERE id = ?", (session_id,))


def change_password(
    username: str,
    current_password: str,
    new_password: str,
    *,
    keep_session_id: str | None,
) -> None:
    if new_password == current_password:
        raise HTTPException(400, "New password must be different")
    if len(new_password) < MIN_NEW_PASSWORD_LENGTH:
        raise HTTPException(400, f"New password must be at least {MIN_NEW_PASSWORD_LENGTH} characters")
    if not authenticate_password(username, current_password):
        raise HTTPException(403, "Current password is incorrect")
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE auth_users SET password_hash = ? WHERE username = ?",
            (hash_password(new_password), username),
        )
        _meta_set(conn, PASSWORD_SOURCE_KEY, "user")
        if keep_session_id:
            conn.execute(
                "DELETE FROM auth_sessions WHERE username = ? AND id != ?",
                (username, keep_session_id),
            )
        else:
            conn.execute("DELETE FROM auth_sessions WHERE username = ?", (username,))
    _write_credentials_file(username, new_password, None)


def authenticate_password(username: str, password: str) -> bool:
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT password_hash FROM auth_users WHERE username = ?",
            (username,),
        ).fetchone()
    if row is None:
        verify_password(password, hash_password("invalid"))
        return False
    return verify_password(password, row["password_hash"])


def _session_identity(session_id: str) -> Identity | None:
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT username, expires_at, csrf_token FROM auth_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            expires = datetime.strptime(row["expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            conn.execute("DELETE FROM auth_sessions WHERE id = ?", (session_id,))
            return None
        if expires <= _now():
            conn.execute("DELETE FROM auth_sessions WHERE id = ?", (session_id,))
            return None
        return Identity(
            username=row["username"],
            via="session",
            csrf_token=row["csrf_token"],
            session_id=session_id,
        )


def _token_identity(token: str) -> Identity | None:
    digest = hash_token(token)
    with db.get_conn() as conn:
        row = conn.execute("SELECT value FROM auth_meta WHERE key = 'api_token_hash'").fetchone()
    if row is None or not hmac.compare_digest(digest, row["value"]):
        return None
    return Identity(username="dispatcher", via="token")


def authenticate_request(request: Request) -> Identity | None:
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        token = header[7:].strip()
        if token:
            return _token_identity(token)
    session_id = request.cookies.get(SESSION_COOKIE)
    if session_id:
        return _session_identity(session_id)
    return None


def is_public_path(path: str) -> bool:
    if path in _PUBLIC_EXACT:
        return True
    return any(path.startswith(prefix) for prefix in _PUBLIC_PREFIXES)


def origin_allowed(request: Request) -> bool:
    origin = request.headers.get("origin")
    referer = request.headers.get("referer")
    host = request.headers.get("host")
    if origin:
        parsed = urlparse(origin)
        origin_host = parsed.netloc
        return bool(host) and origin_host == host
    if referer:
        parsed = urlparse(referer)
        return bool(host) and parsed.netloc == host
    return False


def csrf_allowed(request: Request, identity: Identity) -> bool:
    header = request.headers.get("x-csrf-token") or request.headers.get("x-cairn-csrf")
    cookie = request.cookies.get(CSRF_COOKIE)
    expected = identity.csrf_token
    if not expected or not header or not cookie:
        return False
    return hmac.compare_digest(header, expected) and hmac.compare_digest(cookie, expected)


def apply_security_headers(response: Response) -> None:
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault("Cache-Control", "no-store")
    # Alpine evaluates x-data / x-show / @submit via new Function(), so
    # script-src must allow 'unsafe-eval' or the login form never binds.
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "font-src 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'",
    )


async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if is_public_path(path):
        response = await call_next(request)
        apply_security_headers(response)
        return response

    identity = authenticate_request(request)
    if identity is None:
        response = JSONResponse({"detail": "Not authenticated"}, status_code=401)
        apply_security_headers(response)
        return response

    if identity.via == "session" and request.method not in _SAFE_METHODS:
        if not origin_allowed(request):
            response = JSONResponse({"detail": "Invalid request origin"}, status_code=403)
            apply_security_headers(response)
            return response
        if not csrf_allowed(request, identity):
            response = JSONResponse({"detail": "CSRF check failed"}, status_code=403)
            apply_security_headers(response)
            return response

    request.state.identity = identity
    response = await call_next(request)
    apply_security_headers(response)
    return response


def login_response(request: Request, username: str) -> JSONResponse:
    session_id, csrf_token = create_session(username)
    response = JSONResponse({"username": username, "csrf_token": csrf_token})
    _set_session_cookies(response, request, session_id, csrf_token)
    apply_security_headers(response)
    return response


def logout_response(request: Request) -> Response:
    destroy_session(request.cookies.get(SESSION_COOKIE))
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    apply_security_headers(response)
    return response
