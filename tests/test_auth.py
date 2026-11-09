"""Security integration tests for the trusted-user shared ledger."""
import hashlib
import json
import logging
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import auth, db, trading
from app.main import app

P1 = "Very secure pass 123!"
P2 = "Another secure pass 456!"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_AUTH_ENABLED", "1")
    monkeypatch.setenv("APP_BIND_ADDR", "0.0.0.0")
    monkeypatch.setattr(auth, "limiter", auth.LoginLimiter())
    return tmp_path


@pytest.fixture
def client(env):
    with TestClient(app, follow_redirects=False) as c:
        yield c


def add(username="Admin", password=P1, role="admin"):
    return auth.create_user(username, password, role)


def login(client, username="Admin", password=P1, **kw):
    return client.post("/api/auth/login", json={"username": username, "password": password}, **kw)


def csrf(client):
    return {"X-CSRF-Token": client.get("/api/auth/session").json()["csrf"]}


def test_initial_install_locked_except_health_and_login(client):
    assert client.get("/api/health").status_code == 200
    for url in ("/login", "/static/login.css", "/static/login.js"):
        assert client.get(url).status_code == 200
    assert client.get("/api/auth/session").status_code == 401
    assert client.get("/", follow_redirects=False).status_code == 303
    assert client.get("/").headers["location"] == "/login"
    for url in ("/api/dashboard", "/api/users", "/static/app.js", "/docs", "/openapi.json"):
        assert client.get(url).status_code == 401
    assert login(client).status_code == 401
    assert client.post("/api/users", json={"username":"New", "password":P1, "role":"admin"}).status_code == 401
    assert auth.list_users() == []


def test_login_cookie_storage_expiry_and_session(client, env):
    add()
    r = login(client, "aDMIN")
    assert r.status_code == 200 and r.json() == {"username": "Admin", "role": "admin"}
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Max-Age=43200" in cookie
    assert "Secure" not in cookie
    token = client.cookies.get(auth.COOKIE)
    assert auth.TOKEN.fullmatch(token)
    session = client.get("/api/auth/session").json()
    assert set(session) == {"username", "role", "csrf"} and auth.TOKEN.fullmatch(session["csrf"])
    conn = sqlite3.connect(env / "auth.sqlite3")
    stored, expires = conn.execute("SELECT token_hash,expires_at FROM sessions").fetchone()
    assert stored == hashlib.sha256(token.encode()).digest()
    assert token.encode() not in (env / "auth.sqlite3").read_bytes()
    assert 0 < expires - int(auth.time.time()) <= auth.SESSION_SECONDS
    conn.execute("UPDATE sessions SET expires_at=0")
    conn.commit()
    conn.close()
    assert client.get("/api/auth/session").status_code == 401
    with TestClient(app, base_url="https://testserver") as https:
        assert "Secure" in login(https).headers["set-cookie"]


def test_login_errors_json_limits_no_password_leak(client, caplog):
    add()
    secret = "password-marker-never-leak"
    caplog.set_level(logging.INFO)
    for payload in ({"username":"Admin", "password":secret}, {"username":"missing", "password":secret}):
        r = client.post("/api/auth/login", json=payload)
        assert r.status_code == 401 and r.json() == {"detail":"Invalid username or password."}
        assert secret not in r.text
    r = client.post("/api/auth/login", data={"username":"Admin", "password":secret})
    assert r.status_code == 422
    assert client.post("/api/auth/login", content=b'x' * 4097, headers={"content-type":"application/json"}).status_code == 413
    assert client.post("/api/auth/login", content=b'{"username":"a","username":"b","password":"x"}', headers={"content-type":"application/json"}).status_code == 422
    assert secret not in caplog.text


def test_origin_fetch_site_and_csrf(client):
    add()
    for headers in ({"Origin":"https://evil.invalid"}, {"Origin":"null"}, {"Origin":"https://testserver"},
                    {"Origin":"http://testserver:80"}, {"Origin":"http://testserver/"}, {"Sec-Fetch-Site":"cross-site"},
                    {"X-Forwarded-Proto":"https"}, {"host":"user@testserver"}, {"host":"testserver:"}):
        assert login(client, headers=headers).status_code == 403
    assert login(client, headers={"Origin":"http://testserver", "Sec-Fetch-Site":"same-origin"}).status_code == 200
    token = csrf(client)
    assert client.put("/api/settings", json={"risk_percent":2}).status_code == 403
    assert client.put("/api/settings", json={"risk_percent":2}, headers={"X-CSRF-Token":"invalid"}).status_code == 403
    assert client.put("/api/settings", json={"risk_percent":2}, headers={**token,"Origin":"http://evil.invalid"}).status_code == 403
    assert client.put("/api/settings", json={"risk_percent":2}, headers={**token,"Sec-Fetch-Site":"cross-site"}).status_code == 403
    assert client.put("/api/settings", json={"risk_percent":2}, headers={**token,"Origin":"http://testserver"}).status_code == 200
    assert client.get("/api/dashboard").json()["settings"]["risk_percent"] == 2
    assert client.post("/api/auth/logout", headers=token).status_code == 200
    assert client.get("/api/auth/session").status_code == 401


def test_login_rotation_logout_and_password_revocation(client):
    add()
    assert login(client).status_code == 200
    old_cookie = client.cookies.get(auth.COOKIE)
    old_csrf = csrf(client)
    assert login(client).status_code == 200
    assert client.cookies.get(auth.COOKIE) != old_cookie
    with TestClient(app) as other:
        other.cookies.set(auth.COOKIE, old_cookie)
        assert other.get("/api/auth/session").status_code == 401
    assert client.post("/api/auth/logout", headers=old_csrf).status_code == 403
    assert client.post("/api/users/1/password", json={"password":P2}, headers=csrf(client)).status_code == 200
    assert client.get("/api/auth/session").status_code == 401
    assert login(client, password=P1).status_code == 401
    assert login(client, password=P2).status_code == 200


def test_admin_crud_and_role_permissions(client, monkeypatch):
    admin = add()
    assert login(client).status_code == 200
    h = csrf(client)
    async def fake_scan():
        return {"candidates": [], "warnings": []}
    monkeypatch.setattr(trading, "scan_market", fake_scan)
    r = client.post("/api/users", json={"username":"Trader", "password":P2, "role":"user"}, headers=h)
    assert r.status_code == 200
    trader = r.json()
    assert set(trader) == {"id", "username", "role", "enabled"} and trader["enabled"] is True
    assert client.post("/api/users", json={"username":"trader", "password":P2, "role":"user"}, headers=h).status_code == 409
    assert client.get("/api/users").json()["users"] == [admin, trader]
    assert client.patch(f"/api/users/{admin['id']}", json={"role":"user"}, headers=h).status_code == 409
    assert client.patch(f"/api/users/{admin['id']}", json={"enabled":False}, headers=h).status_code == 409
    assert client.delete(f"/api/users/{admin['id']}", headers=h).status_code == 409
    with TestClient(app) as u:
        assert login(u, "trader", P2).status_code == 200
        uh = csrf(u)
        assert u.get("/api/dashboard").status_code == 200
        assert u.get("/api/users").status_code == 403
        assert u.put("/api/settings", json={"risk_percent":3}, headers=uh).status_code == 403
        assert u.post("/api/llm/openai/analyze", json={"context":"test"}, headers=uh).status_code == 403
        assert u.post("/api/stock-import", json={"csv":"x"}, headers=uh).status_code == 403
        assert u.post("/api/scan", headers=uh).status_code == 200
        assert u.post("/api/cycle", headers=uh).status_code == 200
        trade = {"symbol":"AAPL", "asset_type":"stock", "side":"BUY", "quantity":1, "price":10, "reason":"paper"}
        assert u.post("/api/trades", json=trade, headers=uh).status_code == 200
        pid = u.get("/api/dashboard").json()["positions"][0]["id"]
        assert u.patch(f"/api/positions/{pid}/mark", json={"price":11}, headers=uh).status_code == 200
        assert client.patch(f"/api/users/{trader['id']}", json={"enabled":False}, headers=h).status_code == 200
        assert u.get("/api/auth/session").status_code == 401
    assert client.patch(f"/api/users/{trader['id']}", json={"enabled":True}, headers=h).status_code == 200
    with TestClient(app) as u:
        assert login(u, "trader", P2).status_code == 200
        assert client.patch(f"/api/users/{trader['id']}", json={"role":"admin"}, headers=h).status_code == 200
        assert u.get("/api/auth/session").status_code == 401
    assert client.delete(f"/api/users/{trader['id']}", headers=h).status_code == 200


def test_last_admin_guarantee_is_transactional(env):
    a = add("AdminOne")
    b = add("AdminTwo", P2)
    def demote(i):
        try:
            auth.update_user(user_id=i, role="user")
            return True
        except auth.AuthError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(demote, [a["id"], b["id"]])) == [False, True]
    assert sum(u["enabled"] and u["role"] == "admin" for u in auth.list_users()) == 1


def test_validation_is_strict_and_secret_safe(client):
    add()
    login(client)
    h = csrf(client)
    secret = "secret-password-must-not-appear"
    cases = [
        {"username":"x", "password":secret, "role":"user"},
        {"username":"New", "password":"é"*65, "role":"user"},
        {"username":"New", "password":"short", "role":"user"},
        {"username":"New", "password":secret, "role":"owner"},
        {"username":"New", "password":secret, "role":"user", secret:secret},
    ]
    for payload in cases:
        r = client.post("/api/users", json=payload, headers=h)
        assert r.status_code == 422 and secret not in r.text
    assert client.patch("/api/users/1", json={"enabled":"false"}, headers=h).status_code == 422
    assert client.post("/api/users", content=b"x"*4097, headers={**h,"content-type":"application/json"}).status_code == 413
    assert client.post("/api/users/1/password", content=b'{"password":"\\ud800"}', headers={**h,"content-type":"application/json"}).status_code == 422


def test_password_utf8_byte_boundaries_and_distinct_salts(env):
    short, longest = "é" * 6, "x" * 128
    a = add("abc", short)
    add("z" * 32, longest, "user")
    add("third", short, "user")
    assert auth.authenticate("ABC", short)[1] == {"username": "abc", "role": "admin"}
    assert auth.authenticate("z" * 32, longest)[1]["role"] == "user"
    with sqlite3.connect(env / "auth.sqlite3") as conn:
        first = conn.execute("SELECT salt,password_hash FROM users WHERE id=?", (a["id"],)).fetchone()
        third = conn.execute("SELECT salt,password_hash FROM users WHERE username='third'").fetchone()
    assert first[0] != third[0] and first[1] != third[1]
    for password in ("é" * 5 + "x", "x" * 129):
        with pytest.raises(auth.AuthError):
            add("invalid", password, "user")


def test_rate_limit_client_username_and_bounded_capacity(client):
    add()
    for _ in range(5):
        assert login(client, password="incorrect password").status_code == 401
    assert login(client).status_code == 429
    lim = auth.LoginLimiter(capacity=4, limit=2, window=300)
    lim.failure("client1", "Admin")
    lim.failure("client2", "Admin")
    assert not lim.allowed("client3", "Admin")
    assert lim.allowed("client1", "other")  # one client failure remains below threshold
    assert not lim.allowed("client4", "other")  # never evict counters to make room
    assert len(lim.entries) <= 4
    parallel_limiter = auth.LoginLimiter(limit=5)
    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(lambda _: parallel_limiter.reserve("same-client", "Admin"), range(30)))
    assert sum(results) == 5


def test_database_failure_closed_and_legacy_mode(env, monkeypatch):
    monkeypatch.delenv("APP_AUTH_ENABLED")
    with TestClient(app) as c:
        assert c.get("/api/dashboard").status_code == 401
        assert c.get("/api/health").status_code == 200
    monkeypatch.setenv("APP_AUTH_ENABLED", "0")
    with TestClient(app) as c:
        assert c.get("/api/health").status_code == 200
        assert c.get("/api/dashboard").status_code == 503
        assert c.post("/api/trades", json={}).status_code == 503
    monkeypatch.setenv("APP_BIND_ADDR", "127.0.0.1")
    with TestClient(app) as c:
        assert c.get("/api/auth/session").json() == {"username":"local-dev", "role":"admin", "csrf":""}
        assert c.get("/api/dashboard").status_code == 200
        assert c.get("/api/users").status_code == 503
    monkeypatch.setenv("APP_AUTH_ENABLED", "1")
    monkeypatch.setattr(auth, "ensure_initialized", lambda: (_ for _ in ()).throw(sqlite3.OperationalError("secret")))
    with TestClient(app) as c:
        c.cookies.set(auth.COOKIE, "A"*43)
        assert c.get("/api/dashboard").status_code == 503
        assert c.get("/api/health").status_code == 200
        assert login(c).status_code == 503


def test_preserve_existing_ledger_and_password_hash(env):
    db.ensure_initialized()
    with db.transaction() as conn:
        conn.execute("UPDATE account SET cash='12345.67' WHERE id=1")
    add()
    with db.read_connection() as conn:
        assert conn.execute("SELECT cash FROM account WHERE id=1").fetchone()[0] == "12345.67"
    conn = sqlite3.connect(env / "auth.sqlite3")
    salt, digest = conn.execute("SELECT salt,password_hash FROM users").fetchone()
    assert len(salt) == 32 and digest == hashlib.pbkdf2_hmac("sha256", P1.encode(), salt, 600000)
    assert P1.encode() not in (env / "auth.sqlite3").read_bytes()
    conn.close()


def test_compare_digest_used(client, monkeypatch):
    add()
    calls = []
    original = auth.hmac.compare_digest
    def spy(a, b):
        calls.append((type(a), type(b)))
        return original(a, b)
    monkeypatch.setattr(auth.hmac, "compare_digest", spy)
    assert login(client).status_code == 200
    assert (bytes, bytes) in calls
    assert client.post("/api/auth/logout", headers=csrf(client)).status_code == 200
    assert (str, str) in calls


def test_cli_stdin_and_last_admin(env):
    def cli(*args, data=None):
        return subprocess.run([sys.executable, "-m", "app.user_cli", *args], input=data,
                              text=True, capture_output=True, env=os.environ.copy(), timeout=15)
    assert cli("admin-exists").returncode == 1
    assert cli("create", "Root", "admin", data=P1+"\n").returncode == 0
    assert cli("admin-exists").returncode == 0
    listed = json.loads(cli("list").stdout)["users"]
    assert listed == [{"id":1,"username":"Root","role":"admin","enabled":True}]
    assert cli("disable", "Root").returncode != 0
    assert cli("delete", "Root").returncode != 0
    for data in (P2, P2+"\nextra", "short\n"):
        result = cli("password", "Root", data=data)
        assert result.returncode != 0 and P2 not in result.stdout + result.stderr
    assert cli("password", "Root", data=P2+"\n").returncode == 0
    assert cli("create", "Worker", "user", data=P1+"\n").returncode == 0
    assert cli("disable", "Worker").returncode == 0
    assert cli("enable", "Worker").returncode == 0
    assert cli("delete", "Worker").returncode == 0
