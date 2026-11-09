"""Unit tests use mock provider transports; no real provider or credentials."""
import json
import os
import sqlite3
import stat
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import llm


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("APP_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("APP_PORT", raising=False)
    monkeypatch.delenv("PORT", raising=False)
    app = FastAPI()
    app.include_router(llm.router)
    with TestClient(app) as test_client:
        yield test_client


def install_provider_transport(monkeypatch, handler):
    monkeypatch.setattr(llm, "_http_client", lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False, trust_env=False,
    ))


def test_presets_and_encrypted_key_lifecycle(client, tmp_path):
    listed = client.get("/api/llm").json()
    assert listed["active_provider"] is None
    assert {"openai", "deepseek", "google", "ollama-local", "ollama-cloud", "anthropic", "custom"} <= {p["id"] for p in listed["providers"]}
    assert all(not p["has_key"] for p in listed["providers"])
    local = next(p for p in listed["providers"] if p["id"] == "ollama-local")
    assert local["base_url"] == "http://host.docker.internal:11434/v1"
    assert stat.S_IMODE((tmp_path / ".encryption-key").stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "providers.sqlite3").stat().st_mode) == 0o600

    secret = "test-secret-should-never-be-visible"
    response = client.put("/api/llm/openai", json={"api_key": secret})
    assert response.status_code == 200
    assert response.json()["has_key"] is True
    assert secret not in response.text
    assert secret not in client.get("/api/llm").text
    assert secret.encode() not in (tmp_path / "providers.sqlite3").read_bytes()
    with sqlite3.connect(tmp_path / "providers.sqlite3") as conn:
        encrypted = conn.execute("SELECT encrypted_key FROM providers WHERE id='openai'").fetchone()[0]
    assert Fernet((tmp_path / ".encryption-key").read_bytes()).decrypt(encrypted).decode() == secret

    assert client.put("/api/llm/openai", json={"api_key": ""}).json()["has_key"]
    assert client.put("/api/llm/openai", json={"name": "My provider"}).json()["has_key"]
    assert client.put("/api/llm/openai", json={"clear_key": True}).json()["has_key"] is False


def test_empty_environment_key_generates_key_file(client, tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENCRYPTION_KEY", "")
    assert client.get("/api/llm").status_code == 200
    assert (tmp_path / ".encryption-key").is_file()


def test_environment_key_and_unreadable_key(client, tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENCRYPTION_KEY", Fernet.generate_key().decode())
    assert client.put("/api/llm/openai", json={"api_key": "fake-key"}).status_code == 200
    assert not (tmp_path / ".encryption-key").exists()
    monkeypatch.setenv("APP_ENCRYPTION_KEY", Fernet.generate_key().decode())
    response = client.post("/api/llm/openai/test")
    assert response.status_code == 503
    assert "fake-key" not in response.text
    monkeypatch.setenv("APP_ENCRYPTION_KEY", "invalid")
    assert client.get("/api/llm").status_code == 503


def test_key_file_is_stable_and_concurrent_creation_is_atomic(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("APP_ENCRYPTION_KEY", raising=False)
    with ThreadPoolExecutor(max_workers=8) as pool:
        ciphers = list(pool.map(lambda _: llm._fernet(tmp_path), range(30)))
    token = ciphers[0].encrypt(b"hello")
    assert all(cipher.decrypt(token) == b"hello" for cipher in ciphers)


def test_upsert_and_select_active(client):
    response = client.put("/api/llm/my-provider", json={"name": "Private", "kind": "openai-compatible", "base_url": "http://192.168.1.5:11434/v1/", "model": "my-model", "enabled": True})
    assert response.status_code == 200
    assert response.json() == {"id": "my-provider", "name": "Private", "kind": "openai", "base_url": "http://192.168.1.5:11434/v1", "model": "my-model", "has_key": False, "enabled": True}
    assert client.put("/api/llm/active/select", json={"id": "my-provider"}).json() == {"active_provider": "my-provider"}
    assert client.get("/api/llm").json()["active_provider"] == "my-provider"
    assert client.put("/api/llm/my-provider", json={"enabled": False}).status_code == 200
    assert client.get("/api/llm").json()["active_provider"] is None
    assert client.put("/api/llm/active/select", json={"id": "my-provider"}).status_code == 400
    assert client.put("/api/llm/active/select", json={"id": "no-such-provider"}).status_code == 404
    assert client.put("/api/llm/active/select", json={"id": None}).json() == {"active_provider": None}
    assert client.get("/api/llm").json()["active_provider"] is None


def test_openai_test_is_real_minimal_completion_and_local_needs_no_key(client, monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})
    install_provider_transport(monkeypatch, handler)
    response = client.post("/api/llm/ollama-local/test")
    assert response.json()["ok"] is True
    assert len(calls) == 1
    request = calls[0]
    assert str(request.url) == "http://host.docker.internal:11434/v1/chat/completions"
    assert "authorization" not in request.headers
    body = json.loads(request.content)
    assert body["model"] == "llama3.2"
    assert len(body["messages"]) == 1 and body["messages"][0]["role"] == "user"
    assert body["max_tokens"] == 256


def test_ollama_cloud_and_custom_openai_compatible(client, monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "Analysis text."}}]})
    install_provider_transport(monkeypatch, handler)
    client.put("/api/llm/ollama-cloud", json={"api_key": "fake-cloud-key", "model": "cloud-model"})
    assert client.post("/api/llm/ollama-cloud/test").json()["ok"]
    assert str(calls[-1].url) == "https://ollama.com/v1/chat/completions"
    assert calls[-1].headers["authorization"] == "Bearer fake-cloud-key"
    client.put("/api/llm/custom", json={"base_url": "https://inference.example", "model": "custom-model"})
    result = client.post("/api/llm/custom/analyze", json={"context": "Market context"}).json()
    assert result["ok"] and result["analysis"] == "Analysis text."
    assert str(calls[-1].url) == "https://inference.example/v1/chat/completions"
    payload = json.loads(calls[-1].content)
    assert "Market context" in payload["messages"][0]["content"]
    assert len(payload["messages"]) == 1
    assert "tools" not in payload


def test_google_uses_header_not_query_and_generate_content(client, monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "Gemini response"}]}}]})
    install_provider_transport(monkeypatch, handler)
    client.put("/api/llm/google", json={"api_key": "fake-google-key", "model": "models/gemini-demo"})
    result = client.post("/api/llm/google/analyze", json={"context": "hello"}).json()
    assert result["analysis"] == "Gemini response"
    request = calls[0]
    assert str(request.url) == "https://generativelanguage.googleapis.com/v1beta/models/gemini-demo:generateContent"
    assert request.headers["x-goog-api-key"] == "fake-google-key"
    assert not request.url.query
    assert "hello" in json.loads(request.content)["contents"][0]["parts"][0]["text"]


def test_anthropic_messages(client, monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"content": [{"type": "text", "text": "Anthropic response"}]})
    install_provider_transport(monkeypatch, handler)
    client.put("/api/llm/anthropic", json={"api_key": "fake-anthropic-key", "model": "model-demo"})
    result = client.post("/api/llm/anthropic/test").json()
    assert result["ok"] is True
    req = calls[0]
    assert str(req.url) == "https://api.anthropic.com/v1/messages"
    assert req.headers["x-api-key"] == "fake-anthropic-key"
    assert req.headers["anthropic-version"] == "2023-06-01"
    assert json.loads(req.content)["messages"][0]["content"] == "Reply with OK."


def test_provider_errors_and_echoed_keys_are_sanitized(client, monkeypatch):
    key = "fake-secret-12345"
    client.put("/api/llm/openai", json={"api_key": key})
    def reject(request):
        return httpx.Response(401, text="password is " + key, headers={"location": "https://evil.example"})
    install_provider_transport(monkeypatch, reject)
    result = client.post("/api/llm/openai/test")
    assert result.json() == {"ok": False, "message": "Provider returned HTTP 401.", "status_code": 401}
    assert key not in result.text
    def error(request):
        raise httpx.ConnectError("secret: " + key, request=request)
    install_provider_transport(monkeypatch, error)
    response = client.post("/api/llm/openai/analyze", json={"context": "data"})
    assert not response.json()["ok"] and key not in response.text
    def echo(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "Echo: " + key}}]})
    install_provider_transport(monkeypatch, echo)
    response = client.post("/api/llm/openai/analyze", json={"context": "data"})
    assert response.json()["analysis"] == "Echo: [REDACTED]"
    assert key not in response.text


def test_timeout_invalid_response_missing_key_and_disabled(client, monkeypatch):
    def timeout(request):
        raise httpx.ReadTimeout("ignored details", request=request)
    install_provider_transport(monkeypatch, timeout)
    assert "timed out" in client.post("/api/llm/openai/test").json()["message"]
    def malformed(request):
        return httpx.Response(200, text="bad json")
    install_provider_transport(monkeypatch, malformed)
    assert client.post("/api/llm/openai/test").json()["ok"] is False
    assert "API key" in client.post("/api/llm/google/test").json()["message"]
    client.put("/api/llm/openai", json={"enabled": False})
    assert client.post("/api/llm/openai/analyze", json={"context": "hello"}).json()["message"] == "Provider is disabled."
    # A disabled provider can still be tested before enabling it.
    assert client.post("/api/llm/openai/test").status_code == 200


@pytest.mark.parametrize("url", [
    "http://public.example/v1", "https://user:pass@example.com/v1", "https://example.com/v1?key=secret",
    "https://example.com/v1#fragment", "ftp://example.com/v1", "https:///v1",
    "http://localhost:8000/v1", "http://host.docker.internal:8000/v1", "http://127.0.0.1:8000/v1",
    "http://169.254.169.254/v1", "https://metadata.google.internal/v1", "http://0.0.0.0/v1",
    "https://example.com/api/llm", "https://example.com:bad/v1", "https://example.com\\@localhost/v1",
    "https://example.com:/v1", "https://a..b/v1", "https://[::ffff:127.0.0.1]:8000/v1",
    "https://[::ffff:169.254.169.254]/v1",
])
def test_unsafe_urls_are_rejected(client, url, monkeypatch):
    monkeypatch.setattr(llm.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("8.8.8.8", 80))])
    assert client.put("/api/llm/custom", json={"base_url": url}).status_code == 422


def test_private_dns_and_configured_app_port(client, monkeypatch):
    monkeypatch.setattr(llm.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("10.1.2.3", 80))])
    assert client.put("/api/llm/custom", json={"base_url": "http://lan-host:11434/v1"}).status_code == 200
    monkeypatch.setenv("APP_PORT", "8765")
    assert client.put("/api/llm/custom", json={"base_url": "http://lan-host:8765/v1"}).status_code == 422
    assert client.put("/api/llm/custom", json={"base_url": "https://lan-host:8765/v1"}).status_code == 422
    assert client.put("/api/llm/custom", json={"base_url": "https://public.example/v1"}).status_code == 200
    assert client.put("/api/llm/custom", json={"base_url": "https://public.example/api/llm-proxy/v1"}).status_code == 200


def test_bad_request_never_reflects_key(client):
    secret = "fake-key-in-invalid-input"
    response = client.put("/api/llm/openai", json={"api_key": secret, "unexpected": "value"})
    assert response.status_code == 422 and secret not in response.text
    response = client.put("/api/llm/openai", json={"api_key": secret, "enabled": "not-a-bool"})
    assert response.status_code == 422 and secret not in response.text
    assert client.put("/api/llm/openai", json={"api_key": secret, "clear_key": True}).status_code == 422
    assert client.post("/api/llm/openai/analyze", json={"context": ""}).status_code == 422
    assert client.post("/api/llm/missing/test").status_code == 404


def stored_provider(tmp_path, provider_id):
    with sqlite3.connect(tmp_path / "providers.sqlite3") as conn:
        conn.row_factory = sqlite3.Row
        return dict(conn.execute("SELECT * FROM providers WHERE id=?", (provider_id,)).fetchone())


@pytest.mark.parametrize(("provider_id", "new_url", "kind"), [
    ("openai", "HTTPS://API.OPENAI.COM:443/another/path", "openai-compatible"),
    ("google", "HTTPS://GENERATIVELANGUAGE.GOOGLEAPIS.COM:443/another", "gemini"),
    ("ollama-cloud", "HTTPS://OLLAMA.COM:443/another", "ollama"),
    ("ollama-local", "HTTP://HOST.DOCKER.INTERNAL:11434/another", "ollama-local"),
    ("anthropic", "HTTPS://API.ANTHROPIC.COM:443/another", "anthropic"),
])
def test_same_origin_and_alias_keep_saved_key(client, tmp_path, provider_id, new_url, kind):
    key = "saved-key-same-origin"
    assert client.put(f"/api/llm/{provider_id}", json={"api_key": key}).status_code == 200
    before = stored_provider(tmp_path, provider_id)
    response = client.put(f"/api/llm/{provider_id}", json={
        "base_url": new_url, "kind": kind, "name": "New name", "model": "new-model",
        "api_key": "", "clear_key": False,
    })
    assert response.status_code == 200
    assert response.json()["has_key"] is True
    assert response.json()["base_url"] == new_url
    assert response.json()["name"] == "New name"
    assert response.json()["model"] == "new-model"
    assert key not in response.text
    after = stored_provider(tmp_path, provider_id)
    assert after["encrypted_key"] == before["encrypted_key"]
    assert llm._load_provider(provider_id)[1] == key


@pytest.mark.parametrize("provider_id", ["openai", "google", "ollama-local", "ollama-cloud", "anthropic"])
@pytest.mark.parametrize("change", ["origin", "kind"])
@pytest.mark.parametrize(("key_present", "key_value"), [(False, None), (True, None), (True, ""), (True, "  ")])
def test_hijack_update_rejected_atomically_without_sending_key(client, tmp_path, monkeypatch, provider_id, change, key_present, key_value):
    key = "stored-secret-never-expose"
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "OK"}}],
            "candidates": [{"content": {"parts": [{"text": "OK"}]}}],
            "content": [{"type": "text", "text": "OK"}],
        })

    install_provider_transport(monkeypatch, handler)
    assert client.put(f"/api/llm/{provider_id}", json={"api_key": key, "model": "test-model"}).status_code == 200
    assert client.put("/api/llm/active/select", json={"id": provider_id}).status_code == 200
    before = stored_provider(tmp_path, provider_id)
    changes = {"name": "Attempted name", "model": "attempted-model", "enabled": False, "clear_key": False}
    if key_present:
        changes["api_key"] = key_value
    if change == "origin":
        changes["base_url"] = "https://attacker.example/v1"
    else:
        changes["kind"] = "openai" if before["kind"] != "openai" else "google"
    response = client.put(f"/api/llm/{provider_id}", json=changes)
    assert response.status_code == 409
    assert "replacing or clearing" in response.json()["detail"]
    assert key not in response.text
    assert calls == []
    assert stored_provider(tmp_path, provider_id) == before
    assert client.get("/api/llm").json()["active_provider"] == provider_id

    # Even a subsequent test can only send the original key to the original host.
    result = client.post(f"/api/llm/{provider_id}/test")
    assert result.status_code == 200 and result.json()["ok"] is True
    assert key not in result.text
    assert len(calls) == 1
    assert calls[0].url.host == httpx.URL(before["base_url"]).host
    header = {"openai": "authorization", "google": "x-goog-api-key", "anthropic": "x-api-key"}[before["kind"]]
    assert key in calls[0].headers[header]


@pytest.mark.parametrize(("old_url", "new_url"), [
    ("https://example.com/v1", "https://other.example/v1"),
    ("https://example.com/v1", "https://example.com:444/v1"),
    ("https://example.com/v1", "https://example.com:0/v1"),
    ("http://localhost:11434/v1", "https://localhost:11434/v1"),
    ("https://example.com/v1", "https://example.com./v1"),
    ("https://faß.de/v1", "https://fass.de/v1"),
    ("https://example.com/v1", ""),
    ("", "https://example.com/v1"),
])
def test_origin_changes_including_empty_are_rejected(client, tmp_path, monkeypatch, old_url, new_url):
    calls = []
    install_provider_transport(monkeypatch, lambda request: calls.append(request))
    key = "secret-for-origin-comparison"
    assert client.put("/api/llm/origin-test", json={"base_url": old_url, "model": "model", "api_key": key}).status_code == 200
    before = stored_provider(tmp_path, "origin-test")
    response = client.put("/api/llm/origin-test", json={"base_url": new_url, "api_key": None})
    assert response.status_code == 409
    assert key not in response.text
    assert stored_provider(tmp_path, "origin-test") == before
    assert calls == []


@pytest.mark.parametrize(("old_url", "new_url"), [
    ("http://localhost/v1", "HTTP://LOCALHOST:80/new"),
    ("https://example.com/v1", "HTTPS://EXAMPLE.COM:0443/new"),
    ("https://faß.de/v1", "https://xn--fa-hia.de/new"),
    ("http://[::1]:11434/v1", "http://[0:0:0:0:0:0:0:1]:11434/new"),
])
def test_equivalent_origins_allow_path_updates(client, tmp_path, old_url, new_url):
    assert client.put("/api/llm/equivalent", json={"base_url": old_url, "api_key": "saved-secret"}).status_code == 200
    before = stored_provider(tmp_path, "equivalent")
    response = client.put("/api/llm/equivalent", json={"base_url": new_url})
    assert response.status_code == 200
    assert stored_provider(tmp_path, "equivalent")["encrypted_key"] == before["encrypted_key"]


def test_origin_normalization_never_resolves_dns(monkeypatch):
    def unexpected_dns(*args, **kwargs):
        pytest.fail("Origin comparison must not make DNS requests")

    monkeypatch.setattr(llm.socket, "getaddrinfo", unexpected_dns)
    assert llm._url_origin("HTTPS://FAß.DE:443/path") == ("https", "xn--fa-hia.de", 443)
    assert llm._url_origin("http://EXAMPLE.COM:0/path") == ("http", "example.com", 0)


@pytest.mark.parametrize("bad_url", [
    "https://example.com:bad/v1", "https:///v1", "https://user:pass@example.com/v1",
    "https://example.com../v1", "https://example.com:99999/v1", "https://[v1.abc]/v1",
])
def test_malformed_saved_origin_fails_closed(client, tmp_path, bad_url):
    key = "malformed-origin-secret"
    assert client.put("/api/llm/openai", json={"api_key": key}).status_code == 200
    with sqlite3.connect(tmp_path / "providers.sqlite3") as conn:
        conn.execute("UPDATE providers SET base_url=? WHERE id='openai'", (bad_url,))
    before = stored_provider(tmp_path, "openai")
    response = client.put("/api/llm/openai", json={"name": "must not be saved"})
    assert response.status_code == 409
    assert key not in response.text
    assert stored_provider(tmp_path, "openai") == before
    # An explicit clear lets the user repair a bad legacy record safely.
    repaired = client.put("/api/llm/openai", json={"base_url": "https://api.openai.com/v1", "clear_key": True})
    assert repaired.status_code == 200 and repaired.json()["has_key"] is False


@pytest.mark.parametrize("bad_url", ["https://example.com../v1", "https://[v1.abc]/v1"])
@pytest.mark.parametrize("key_change", [{}, {"api_key": "new-secret"}, {"clear_key": True}])
def test_malformed_new_origin_fails_closed(client, tmp_path, bad_url, key_change):
    assert client.put("/api/llm/openai", json={"api_key": "protected-secret"}).status_code == 200
    before = stored_provider(tmp_path, "openai")
    response = client.put("/api/llm/openai", json={"base_url": bad_url, **key_change})
    assert response.status_code == 422
    assert "protected-secret" not in response.text
    assert "new-secret" not in response.text
    assert stored_provider(tmp_path, "openai") == before


@pytest.mark.parametrize("provider_id", ["openai", "google", "ollama-local", "ollama-cloud", "anthropic"])
@pytest.mark.parametrize("action", ["replace", "clear"])
def test_explicit_key_replacement_or_clear_allows_origin_and_kind_change(client, tmp_path, monkeypatch, provider_id, action):
    calls = []
    old_key, new_key = "old-provider-secret", "replacement-provider-secret"

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "OK"}}],
            "content": [{"type": "text", "text": "OK"}],
        })

    install_provider_transport(monkeypatch, handler)
    assert client.put(f"/api/llm/{provider_id}", json={"api_key": old_key, "model": "test-model"}).status_code == 200
    original = stored_provider(tmp_path, provider_id)
    new_kind = "anthropic" if original["kind"] == "openai" else "openai"
    changes = {"base_url": "https://new.example/v2", "kind": new_kind}
    changes.update({"api_key": new_key} if action == "replace" else {"clear_key": True})
    response = client.put(f"/api/llm/{provider_id}", json=changes)
    assert response.status_code == 200
    assert old_key not in response.text and new_key not in response.text
    assert response.json()["base_url"] == "https://new.example/v2"
    assert response.json()["kind"] == new_kind
    assert response.json()["has_key"] == (action == "replace")
    saved = stored_provider(tmp_path, provider_id)
    assert saved["encrypted_key"] != original["encrypted_key"]
    assert llm._load_provider(provider_id)[1] == (new_key if action == "replace" else None)

    result = client.post(f"/api/llm/{provider_id}/test")
    assert old_key not in result.text and new_key not in result.text
    if action == "clear" and new_kind == "anthropic":
        assert not result.json()["ok"] and calls == []
    else:
        assert result.json()["ok"] and len(calls) == 1
        assert calls[0].url.host == "new.example"
        assert all(old_key not in value for value in calls[0].headers.values())
        if action == "replace":
            header = "x-api-key" if new_kind == "anthropic" else "authorization"
            assert new_key in calls[0].headers[header]
        else:
            assert "authorization" not in calls[0].headers


def test_unkeyed_and_unconfigured_provider_backwards_compatibility(client, tmp_path):
    assert client.put("/api/llm/custom", json={"base_url": "https://first.example", "kind": "google"}).status_code == 200
    assert client.put("/api/llm/custom", json={"base_url": "https://second.example", "kind": "openai"}).status_code == 200
    assert client.put("/api/llm/unconfigured", json={"api_key": "preconfigured-key"}).status_code == 200
    before = stored_provider(tmp_path, "unconfigured")
    response = client.put("/api/llm/unconfigured", json={"name": "Local name", "model": "local-model", "api_key": None})
    assert response.status_code == 200 and response.json()["has_key"] is True
    assert stored_provider(tmp_path, "unconfigured")["encrypted_key"] == before["encrypted_key"]
