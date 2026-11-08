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
