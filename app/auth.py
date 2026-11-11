"""Authentication for a shared, trusted-user paper ledger (not tenant isolation).

The independent SQLite store leaves existing ledger/provider records untouched.
There is no web bootstrap: create the first administrator with app.user_cli.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

USERNAME = re.compile(r"[A-Za-z0-9._-]{3,32}\Z", re.ASCII)
TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z", re.ASCII)
ITERATIONS = 600_000
SESSION_SECONDS = 12 * 60 * 60
COOKIE = "paperledger_session"
AUTH_BODY_LIMIT = 4096
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


class AuthError(Exception):
    def __init__(self, message: str, status: int = 422):
        self.message = message
        self.status = status
        super().__init__(message)


def enabled() -> bool:
    # Only an explicit zero disables authentication; malformed values fail safe.
    return os.environ.get("APP_AUTH_ENABLED", "1") != "0"


def invalid_disabled_binding() -> bool:
    return not enabled() and os.environ.get("APP_BIND_ADDR", "127.0.0.1") != "127.0.0.1"


def _connect() -> sqlite3.Connection:
    directory = Path(os.environ.get("DATA_DIR", "./data")).expanduser()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / "auth.sqlite3"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)
    conn = sqlite3.connect(str(path), timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def ensure_initialized() -> None:
    conn = _connect()
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL COLLATE NOCASE UNIQUE,
            role TEXT NOT NULL CHECK(role IN ('admin','user')),
            enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
            salt BLOB NOT NULL, password_hash BLOB NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS sessions (
            token_hash BLOB PRIMARY KEY, user_id INTEGER NOT NULL,
            csrf TEXT NOT NULL, expires_at INTEGER NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS sessions_user_id ON sessions(user_id)")
    finally:
        conn.close()


@contextmanager
def _transaction():
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def valid_username(value) -> bool:
    return isinstance(value, str) and USERNAME.fullmatch(value) is not None


def valid_password(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return 12 <= len(value.encode("utf-8")) <= 128
    except UnicodeEncodeError:
        return False


def _password_hash(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)


def _safe(row) -> dict:
    return {"id": row["id"], "username": row["username"],
            "role": row["role"], "enabled": bool(row["enabled"])}


def list_users() -> list[dict]:
    ensure_initialized()
    conn = _connect()
    try:
        return [_safe(row) for row in conn.execute("SELECT id,username,role,enabled FROM users ORDER BY id")]
    finally:
        conn.close()


def create_user(username, password, role) -> dict:
    if not valid_username(username) or not valid_password(password) or role not in ("admin", "user"):
        raise AuthError("Invalid account data.")
    salt = secrets.token_bytes(32)
    password_hash = _password_hash(password, salt)
    ensure_initialized()
    try:
        with _transaction() as conn:
            cur = conn.execute("INSERT INTO users(username,role,enabled,salt,password_hash) VALUES(?,?,1,?,?)",
                               (username, role, salt, password_hash))
            return _safe(conn.execute("SELECT id,username,role,enabled FROM users WHERE id=?", (cur.lastrowid,)).fetchone())
    except sqlite3.IntegrityError:
        raise AuthError("Unable to create account.", 409) from None


def _find_user(conn, *, user_id=None, username=None):
    if user_id is not None:
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    else:
        if not valid_username(username):
            raise AuthError("Invalid username.")
        row = conn.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE", (username,)).fetchone()
    if not row:
        raise AuthError("Account not found.", 404)
    return row


def _check_last_admin(conn, row):
    if row["enabled"] and row["role"] == "admin":
        if conn.execute("SELECT count(*) FROM users WHERE enabled=1 AND role='admin'").fetchone()[0] <= 1:
            raise AuthError("At least one enabled administrator is required.", 409)


def update_user(*, user_id=None, username=None, role=None, enabled_value=None, actor_id=None) -> dict:
    if role is not None and role not in ("admin", "user"):
        raise AuthError("Invalid account data.")
    if enabled_value is not None and not isinstance(enabled_value, bool):
        raise AuthError("Invalid account data.")
    if role is None and enabled_value is None:
        raise AuthError("Provide an account change.")
    ensure_initialized()
    with _transaction() as conn:
        row = _find_user(conn, user_id=user_id, username=username)
        new_role = role if role is not None else row["role"]
        new_enabled = int(enabled_value) if enabled_value is not None else row["enabled"]
        removing_admin = row["role"] == "admin" and row["enabled"] and (new_role != "admin" or not new_enabled)
        if actor_id == row["id"] and (new_role != "admin" or not new_enabled):
            raise AuthError("Cannot disable or demote your own account.", 409)
        if removing_admin:
            _check_last_admin(conn, row)
        conn.execute("UPDATE users SET role=?,enabled=? WHERE id=?", (new_role, new_enabled, row["id"]))
        if row["role"] != new_role or row["enabled"] != new_enabled:
            conn.execute("DELETE FROM sessions WHERE user_id=?", (row["id"],))
        return _safe(conn.execute("SELECT id,username,role,enabled FROM users WHERE id=?", (row["id"],)).fetchone())


def delete_user(*, user_id=None, username=None, actor_id=None) -> None:
    ensure_initialized()
    with _transaction() as conn:
        row = _find_user(conn, user_id=user_id, username=username)
        if actor_id == row["id"]:
            raise AuthError("Cannot delete your own account.", 409)
        _check_last_admin(conn, row)
        conn.execute("DELETE FROM users WHERE id=?", (row["id"],))


def reset_password(password, *, user_id=None, username=None) -> None:
    if not valid_password(password):
        raise AuthError("Invalid account data.")
    salt = secrets.token_bytes(32)
    password_hash = _password_hash(password, salt)
    ensure_initialized()
    with _transaction() as conn:
        row = _find_user(conn, user_id=user_id, username=username)
        conn.execute("UPDATE users SET salt=?,password_hash=? WHERE id=?", (salt, password_hash, row["id"]))
        conn.execute("DELETE FROM sessions WHERE user_id=?", (row["id"],))


def active_admin_exists() -> bool:
    ensure_initialized()
    conn = _connect()
    try:
        return conn.execute("SELECT 1 FROM users WHERE enabled=1 AND role='admin' LIMIT 1").fetchone() is not None
    finally:
        conn.close()


def _digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("ascii")).digest()


def get_session(token):
    if not isinstance(token, str) or not TOKEN.fullmatch(token):
        return None
    ensure_initialized()
    conn = _connect()
    try:
        row = conn.execute("""SELECT u.id,u.username,u.role,s.csrf FROM sessions s
            JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>? AND u.enabled=1""",
            (_digest(token), int(time.time()))).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# Verify an unknown user with the same costly algorithm, avoiding account enumeration.
_DUMMY_SALT = bytes(32)
_DUMMY_HASH = bytes(32)


def authenticate(username, password, old_token=None):
    ensure_initialized()
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE", (username,)).fetchone() if valid_username(username) else None
    finally:
        conn.close()
    candidate = password if valid_password(password) else "invalid-password"
    expected = row["password_hash"] if row else _DUMMY_HASH
    actual = _password_hash(candidate, row["salt"] if row else _DUMMY_SALT)
    match = hmac.compare_digest(actual, expected)
    if not row or not row["enabled"] or not valid_password(password) or not match:
        return None
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    with _transaction() as conn:
        # Prevent an old password from winning a race with a password reset or disable.
        current = conn.execute("SELECT * FROM users WHERE id=?", (row["id"],)).fetchone()
        if not current or not current["enabled"] or not hmac.compare_digest(current["password_hash"], expected) or current["salt"] != row["salt"]:
            return None
        conn.execute("DELETE FROM sessions WHERE expires_at<=?", (int(time.time()),))
        if isinstance(old_token, str) and TOKEN.fullmatch(old_token):
            conn.execute("DELETE FROM sessions WHERE token_hash=?", (_digest(old_token),))
        conn.execute("INSERT INTO sessions(token_hash,user_id,csrf,expires_at) VALUES(?,?,?,?)",
                     (_digest(token), row["id"], csrf, int(time.time()) + SESSION_SECONDS))
        return token, {"username": current["username"], "role": current["role"]}


def revoke_session(token):
    if isinstance(token, str) and TOKEN.fullmatch(token):
        ensure_initialized()
        with _transaction() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash=?", (_digest(token),))


class LoginLimiter:
    """Per-client and per-username bad-login limits with bounded, fail-closed memory.

    In-process, so one worker is recommended; this is not a distributed limiter.
    """
    def __init__(self, capacity=4096, limit=5, window=300):
        self.capacity, self.limit, self.window = capacity, limit, window
        self.entries = OrderedDict()
        self.lock = threading.Lock()

    def _keys(self, client, username):
        # Do not retain raw submitted usernames in the rate-limit table.
        name = username.lower() if valid_username(username) else "<invalid>"
        return ("c:" + str(client)[:128], "u:" + hashlib.sha256(name.encode()).hexdigest())

    def _prune(self, now):
        for key, (_, until) in list(self.entries.items()):
            if until <= now:
                del self.entries[key]

    def allowed(self, client, username):
        now = time.monotonic()
        with self.lock:
            self._prune(now)
            keys = self._keys(client, username)
            missing = sum(key not in self.entries for key in keys)
            if len(self.entries) + missing > self.capacity:
                return False
            return all(self.entries.get(key, (0, 0))[0] < self.limit for key in keys)

    def reserve(self, client, username):
        # Reserve before PBKDF2: a burst cannot bypass limits by racing checks.
        now = time.monotonic()
        with self.lock:
            self._prune(now)
            keys = self._keys(client, username)
            if len(self.entries) + sum(key not in self.entries for key in keys) > self.capacity:
                return False
            if any(self.entries.get(key, (0, 0))[0] >= self.limit for key in keys):
                return False
            for key in keys:
                count, until = self.entries.get(key, (0, now + self.window))
                self.entries[key] = (count + 1, until)
            return True

    def release(self, client, username):
        with self.lock:
            for key in self._keys(client, username):
                if key in self.entries:
                    count, until = self.entries[key]
                    if count <= 1:
                        del self.entries[key]
                    else:
                        self.entries[key] = (count - 1, until)

    def failure(self, client, username):
        now = time.monotonic()
        with self.lock:
            self._prune(now)
            for key in self._keys(client, username):
                if key not in self.entries and len(self.entries) >= self.capacity:
                    continue
                count, until = self.entries.get(key, (0, now + self.window))
                self.entries[key] = (count + 1, until)


limiter = LoginLimiter()
router = APIRouter()


def _one_header(request: Request, name: str):
    values = request.headers.getlist(name)
    if len(values) > 1:
        raise ValueError("duplicate header")
    return values[0] if values else None


def _origin_ok(request: Request) -> bool:
    try:
        origin = _one_header(request, "origin")
        site = _one_header(request, "sec-fetch-site")
        host = _one_header(request, "host")
        if site is not None and site.lower() not in ("same-origin", "none"):
            return False
        if not host or len(host) > 255 or any(ord(c) < 33 or ord(c) >= 127 for c in host):
            return False
        authority = urlsplit("//" + host)
        if (not authority.hostname or authority.username is not None or authority.password is not None
                or authority.netloc != host or authority.path or authority.query or authority.fragment
                or host.endswith(":") or "\\" in host or "%" in host):
            return False
        _ = authority.port
        # Do not trust supplied proxy headers. Uvicorn must also start with
        # --no-proxy-headers, because it otherwise mutates ASGI scope first.
        if any(request.headers.getlist(h) for h in ("forwarded", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "x-forwarded-port")):
            return False
        if origin is None:
            return True
        if len(origin) > 512 or any(ord(c) < 33 or ord(c) >= 127 for c in origin) or "\\" in origin:
            return False
        parsed = urlsplit(origin)
        if (parsed.scheme not in ("http", "https") or parsed.scheme != request.scope.get("scheme")
                or not parsed.hostname or parsed.username is not None or parsed.password is not None
                or parsed.netloc.lower() != host.lower() or parsed.path or parsed.query or parsed.fragment
                or parsed.netloc.endswith(":")):
            return False
        _ = parsed.port  # reject invalid ports
        return True
    except (ValueError, UnicodeError):
        return False


def _json_response(status, detail):
    return JSONResponse({"detail": detail}, status_code=status)


async def _limited_body(receive, send, scope):
    chunks, size = [], 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return None
        chunk = message.get("body", b"")
        size += len(chunk)
        if size > AUTH_BODY_LIMIT:
            await _json_response(413, "Request is too large.")(scope, receive, send)
            return None
        chunks.append(chunk)
        if not message.get("more_body", False):
            break
    body = b"".join(chunks)
    delivered = False

    async def replay():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()
    return replay


class AuthGuardMiddleware:
    """Outer ASGI guard: reject before routing, validation, or any ledger writes."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "websocket":
            # No WebSocket routes exist; do not permit future routes to bypass
            # this HTTP-only authentication and CSRF policy.
            await send({"type": "websocket.close", "code": 4401})
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        path, method = scope["path"], scope["method"].upper()

        async def safe_send(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                if not any(k.lower() == b"cache-control" for k, _ in headers):
                    headers.append((b"cache-control", b"no-store"))
                headers.extend([(b"x-content-type-options", b"nosniff"), (b"x-frame-options", b"DENY"), (b"referrer-policy", b"no-referrer")])
                message = {**message, "headers": headers}
            await send(message)

        async def reject(status, message):
            await _json_response(status, message)(scope, receive, safe_send)

        if path == "/api/health" and method in ("GET", "HEAD"):
            await self.app(scope, receive, safe_send)
            return
        if invalid_disabled_binding():
            await reject(503, "Authentication is required for network access.")
            return
        unsafe = method not in ("GET", "HEAD", "OPTIONS")
        if unsafe and not _origin_ok(request):
            await reject(403, "Cross-origin requests are not allowed.")
            return
        # Reject proxy headers on *every* request: an ASGI server may have
        # already interpreted a forged scheme or client address.
        if any(request.headers.getlist(h) for h in ("forwarded", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "x-forwarded-port")):
            await reject(403, "Proxy headers are not accepted.")
            return
        if not enabled():
            if path == "/api/auth/session" and method == "GET":
                scope.setdefault("state", {})["auth_session"] = {"username": "local-dev", "role": "admin", "csrf": ""}
                await self.app(scope, receive, safe_send)
                return
            if path.startswith("/api/auth/") or path == "/api/users" or path.startswith("/api/users/"):
                await reject(503, "Authentication is disabled.")
                return
            await self.app(scope, receive, safe_send)
            return
        public = ((path in ("/login", "/static/login.css", "/static/login.js") and method in ("GET", "HEAD"))
                  or (path == "/api/auth/login" and method == "POST"))
        session = None
        if not public:
            try:
                session = await asyncio.to_thread(get_session, request.cookies.get(COOKIE))
            except (OSError, sqlite3.Error, ValueError):
                await reject(503, "Authentication storage is unavailable.")
                return
            if not session:
                if path == "/" and method in ("GET", "HEAD"):
                    await RedirectResponse("/login", status_code=303)(scope, receive, safe_send)
                else:
                    await reject(401, "Authentication required.")
                return
            scope.setdefault("state", {})["auth_session"] = session
            if (path == "/api/users" or path.startswith("/api/users/")) and session["role"] != "admin":
                await reject(403, "Administrator access required.")
                return
            if unsafe:
                try:
                    csrf = _one_header(request, "x-csrf-token")
                    if not isinstance(csrf, str) or not TOKEN.fullmatch(csrf) or not hmac.compare_digest(csrf, session["csrf"]):
                        await reject(403, "Invalid CSRF token.")
                        return
                except (ValueError, TypeError):
                    await reject(403, "Invalid CSRF token.")
                    return
                allowed_user = ((method == "POST" and path in ("/api/scan", "/api/cycle", "/api/trades", "/api/auth/logout", "/api/simulation/stocks/cycle"))
                                or (method == "PATCH" and re.fullmatch(r"/api/positions/[0-9]+/mark", path)))
                if session["role"] != "admin" and not allowed_user:
                    await reject(403, "Administrator access required.")
                    return
        if unsafe and (path == "/api/auth/login" or path == "/api/users" or path.startswith("/api/users/")):
            replay = await _limited_body(receive, safe_send, scope)
            if replay is None:
                return
            receive = replay
        await self.app(scope, receive, safe_send)


async def _json_object(request):
    try:
        if (_one_header(request, "content-type") or "").split(";", 1)[0].strip().lower() != "application/json":
            raise ValueError
        raw = await request.body()
        # Reject repeated keys rather than accepting ambiguous account fields.
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError
                result[key] = value
            return result
        data = json.loads(raw, object_pairs_hook=unique)
        if not isinstance(data, dict):
            raise ValueError
        return data
    except (ValueError, UnicodeError, RecursionError):
        raise HTTPException(422, "Invalid account data.") from None


def _handle_error(exc):
    if isinstance(exc, AuthError):
        raise HTTPException(exc.status, exc.message) from None
    raise HTTPException(503, "Authentication storage is unavailable.") from None


@router.get("/login")
def login_page():
    path = STATIC_DIR / "login.html"
    if not path.is_file():
        raise HTTPException(503, "Login page is unavailable.")
    return FileResponse(path)


@router.post("/api/auth/login")
async def login(request: Request):
    client = request.scope.get("client")
    client_id = client[0] if client else "unknown"
    try:
        data = await _json_object(request)
        username, password = data.get("username"), data.get("password")
    except HTTPException:
        if not limiter.reserve(client_id, None):
            raise HTTPException(429, "Too many login attempts.") from None
        raise HTTPException(422, "Invalid account data.") from None
    if not limiter.reserve(client_id, username):
        raise HTTPException(429, "Too many login attempts.")
    try:
        if set(data) != {"username", "password"}:
            password = None
        result = await asyncio.to_thread(authenticate, username, password, request.cookies.get(COOKIE))
    except (OSError, sqlite3.Error, ValueError):
        limiter.release(client_id, username)
        raise HTTPException(503, "Authentication storage is unavailable.") from None
    if not result:
        raise HTTPException(401, "Invalid username or password.")
    limiter.release(client_id, username)
    token, user = result
    response = JSONResponse(user)
    response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, httponly=True,
                        secure=request.scope.get("scheme") == "https", samesite="strict", path="/")
    return response


@router.get("/api/auth/session")
def session(request: Request):
    info = request.state.auth_session
    return {"username": info["username"], "role": info["role"], "csrf": info["csrf"]}


@router.post("/api/auth/logout")
def logout(request: Request):
    try:
        revoke_session(request.cookies.get(COOKIE))
    except (OSError, sqlite3.Error, ValueError) as exc:
        _handle_error(exc)
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE, path="/", secure=request.scope.get("scheme") == "https", httponly=True, samesite="strict")
    return response


@router.get("/api/users")
def users():
    try:
        return {"users": list_users()}
    except (OSError, sqlite3.Error, ValueError) as exc:
        _handle_error(exc)


@router.post("/api/users")
async def add_user(request: Request):
    data = await _json_object(request)
    if set(data) != {"username", "password", "role"}:
        raise HTTPException(422, "Invalid account data.")
    try:
        return await asyncio.to_thread(create_user, data["username"], data["password"], data["role"])
    except (AuthError, OSError, sqlite3.Error, ValueError) as exc:
        _handle_error(exc)


@router.patch("/api/users/{user_id}")
async def patch_user(user_id: int, request: Request):
    data = await _json_object(request)
    if (not data or set(data) - {"role", "enabled"}
            or ("role" in data and data["role"] not in ("admin", "user"))
            or ("enabled" in data and not isinstance(data["enabled"], bool))):
        raise HTTPException(422, "Invalid account data.")
    try:
        return await asyncio.to_thread(update_user, user_id=user_id, role=data.get("role"), enabled_value=data.get("enabled"), actor_id=request.state.auth_session["id"])
    except (AuthError, OSError, sqlite3.Error, ValueError) as exc:
        _handle_error(exc)


@router.post("/api/users/{user_id}/password")
async def change_password(user_id: int, request: Request):
    data = await _json_object(request)
    if set(data) != {"password"}:
        raise HTTPException(422, "Invalid account data.")
    try:
        await asyncio.to_thread(reset_password, data["password"], user_id=user_id)
        return {"ok": True}
    except (AuthError, OSError, sqlite3.Error, ValueError) as exc:
        _handle_error(exc)


@router.delete("/api/users/{user_id}")
def remove_user(user_id: int, request: Request):
    try:
        delete_user(user_id=user_id, actor_id=request.state.auth_session["id"])
        return {"ok": True}
    except (AuthError, OSError, sqlite3.Error, ValueError) as exc:
        _handle_error(exc)
