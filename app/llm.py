"""Configurable model-provider API.

Personal project; educational use only. Nothing here is investment advice.

Mount ``router`` on a FastAPI app. Provider credentials are encrypted with a
Fernet key from APP_ENCRYPTION_KEY, or a generated DATA_DIR/.encryption-key.
A key stored alongside the database protects only against casual disclosure,
not an attacker who can read the volume. This API assumes a trusted, single
user: configurable network endpoints inherently carry SSRF and data-egress
risks. URL checks here are defense in depth, not a network sandbox.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import sqlite3
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api/llm", tags=["llm"])

# A base URL is a provider's API prefix, not its complete request endpoint.
_PRESETS = (
    ("openai", "OpenAI", "openai", "https://api.openai.com/v1", "gpt-4o-mini"),
    ("deepseek", "DeepSeek", "openai", "https://api.deepseek.com/v1", "deepseek-chat"),
    ("google", "Google Gemini", "google", "https://generativelanguage.googleapis.com/v1beta", "gemini-2.5-flash"),
    ("ollama-local", "Ollama Local", "openai", "http://host.docker.internal:11434/v1", "llama3.2"),
    ("ollama-cloud", "Ollama Cloud", "openai", "https://ollama.com/v1", ""),
    ("anthropic", "Anthropic", "anthropic", "https://api.anthropic.com/v1", "claude-sonnet-4-5"),
    ("groq", "Groq", "openai", "https://api.groq.com/openai/v1", ""),
    ("openrouter", "OpenRouter", "openai", "https://openrouter.ai/api/v1", ""),
    ("xai", "xAI", "openai", "https://api.x.ai/v1", ""),
    ("mistral", "Mistral", "openai", "https://api.mistral.ai/v1", ""),
    ("together", "Together AI", "openai", "https://api.together.xyz/v1", ""),
    ("custom", "Custom OpenAI-compatible", "openai", "", ""),
)
_KIND_ALIASES = {
    "openai": "openai", "openai-compatible": "openai", "openai_compatible": "openai",
    "custom": "openai", "deepseek": "openai", "ollama": "openai",
    "ollama-local": "openai", "ollama-cloud": "openai", "groq": "openai",
    "xai": "openai", "openrouter": "openai", "google": "google",
    "gemini": "google", "anthropic": "anthropic",
}
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_PRIVATE_NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8",
    "::1/128", "fc00::/7",
))
_MAX_BODY = 100_000


def _data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", "./data")).expanduser()


def _fernet(directory: Path) -> Fernet:
    """Use an explicit key, or atomically publish a newly generated one."""
    configured = os.environ.get("APP_ENCRYPTION_KEY")
    if configured:
        try:
            return Fernet(configured.encode("ascii"))
        except (ValueError, UnicodeError, TypeError):
            raise HTTPException(503, "Invalid APP_ENCRYPTION_KEY configuration.") from None

    path = directory / ".encryption-key"
    if not path.exists():
        # link() publishes only fully-written bytes and does not overwrite a
        # concurrent worker's key. The temp and final names share a volume.
        fd, tmp = tempfile.mkstemp(prefix=".encryption-key-", dir=directory)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(Fernet.generate_key())
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(tmp, path, follow_symlinks=False)
            except FileExistsError:
                pass
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "rb") as handle:
            key = handle.read(128).strip()
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    try:
        return Fernet(key)
    except (ValueError, TypeError):
        raise HTTPException(503, "Invalid encryption key file.") from None


def _open_store() -> tuple[sqlite3.Connection, Fernet]:
    try:
        directory = _data_dir()
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        cipher = _fernet(directory)
        db_path = directory / "providers.sqlite3"
        fd = os.open(db_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        conn = sqlite3.connect(str(db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("""CREATE TABLE IF NOT EXISTS providers (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
                base_url TEXT NOT NULL, model TEXT NOT NULL,
                encrypted_key BLOB, enabled INTEGER NOT NULL DEFAULT 1
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY, value TEXT
            )""")
            conn.executemany(
                "INSERT OR IGNORE INTO providers (id,name,kind,base_url,model,enabled) VALUES (?,?,?,?,?,1)",
                _PRESETS,
            )
            conn.commit()
        except Exception:
            conn.close()
            raise
        return conn, cipher
    except HTTPException:
        raise
    except (OSError, sqlite3.Error, ValueError):
        raise HTTPException(503, "Provider storage is unavailable.") from None


def _provider(row: sqlite3.Row) -> dict[str, Any]:
    return {"id": row["id"], "name": row["name"], "kind": row["kind"],
            "base_url": row["base_url"], "model": row["model"],
            "has_key": bool(row["encrypted_key"]), "enabled": bool(row["enabled"])}


def _valid_id(value: str) -> str:
    if not _ID_RE.fullmatch(value):
        raise HTTPException(422, "Invalid provider ID.")
    return value


def _private_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip.version == net.version and ip in net for net in _PRIVATE_NETWORKS)


def _url_origin(value: str) -> tuple[str, str, int] | None:
    """Canonicalize an origin without DNS; reject malformed saved URLs.

    The empty URL is an unconfigured destination, not a wildcard origin.
    Use HTTPX's wire hostname for IDNs so that distinct destinations are not
    collapsed by Python's different IDNA codec. Preserve a trailing DNS dot:
    HTTP virtual hosts may distinguish it even if DNS resolves it identically.
    """
    if value == "":
        return None
    try:
        if (not isinstance(value, str) or len(value) > 2048
                or any(ord(c) < 33 or ord(c) == 127 for c in value) or "\\" in value):
            raise ValueError
        parts = urlsplit(value)
        host, port = parts.hostname, parts.port
        if (parts.scheme not in ("http", "https") or not host or not parts.netloc
                or parts.username is not None or parts.password is not None
                or parts.query or parts.fragment or "%" in host or parts.netloc.endswith(":")):
            raise ValueError
        wire_host = httpx.URL(value).raw_host.decode("ascii").lower()
        try:
            wire_host = str(ipaddress.ip_address(wire_host))
        except ValueError:
            dns_host = wire_host[:-1] if wire_host.endswith(".") else wire_host
            labels = dns_host.split(".")
            if len(dns_host) > 253 or any(
                not re.fullmatch(r"[a-z0-9](?:[a-z0-9_-]{0,61}[a-z0-9])?", label)
                for label in labels
            ):
                raise ValueError
        effective_port = port if port is not None else (443 if parts.scheme == "https" else 80)
        return parts.scheme, wire_host, effective_port
    except (ValueError, UnicodeError, TypeError, httpx.InvalidURL):
        raise ValueError("Invalid provider URL origin") from None


def _validate_url(value: str) -> str:
    if not value:
        return ""
    try:
        _url_origin(value)
    except ValueError:
        raise HTTPException(422, "Invalid provider base URL.") from None
    if len(value) > 2048 or any(ord(c) < 33 or ord(c) == 127 for c in value) or "\\" in value:
        raise HTTPException(422, "Invalid provider base URL.")
    try:
        parts = urlsplit(value)
        host = parts.hostname
        port = parts.port
    except ValueError:
        raise HTTPException(422, "Invalid provider base URL.") from None
    if (parts.scheme not in ("http", "https") or not host or not parts.netloc
            or parts.username is not None or parts.password is not None
            or parts.query or parts.fragment or "%" in host or parts.netloc.endswith(":")):
        raise HTTPException(422, "Invalid provider base URL.")
    host = host.lower().rstrip(".")
    path = parts.path.lower().rstrip("/")
    if host in ("metadata", "metadata.google.internal") or path == "/api/llm" or path.startswith("/api/llm/"):
        raise HTTPException(422, "Provider base URL is not allowed.")
    try:
        parsed_ip = ipaddress.ip_address(host)
    except ValueError:
        parsed_ip = None
        try:
            ascii_host = host.encode("idna").decode("ascii")
        except UnicodeError:
            raise HTTPException(422, "Invalid provider base URL.") from None
        labels = ascii_host.split(".")
        if len(ascii_host) > 253 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9_-]{0,61}[a-z0-9])?", label) for label in labels):
            raise HTTPException(422, "Invalid provider base URL.")
    if isinstance(parsed_ip, ipaddress.IPv6Address) and parsed_ip.ipv4_mapped:
        parsed_ip = parsed_ip.ipv4_mapped
    if parsed_ip and (parsed_ip.is_link_local or parsed_ip.is_unspecified or parsed_ip.is_multicast):
        raise HTTPException(422, "Provider base URL is not allowed.")

    local = host in ("localhost", "host.docker.internal") or bool(parsed_ip and _private_ip(parsed_ip))
    if parts.scheme == "http" and not local:
        # Support resolvable private Docker/LAN names. All addresses must be
        # private; DNS can still change between validation and connection.
        if parsed_ip:
            raise HTTPException(422, "HTTP is allowed only for local or private-network providers.")
        try:
            addresses = {ipaddress.ip_address(item[4][0]) for item in socket.getaddrinfo(host, port if port is not None else 80, type=socket.SOCK_STREAM)}
        except (OSError, ValueError):
            addresses = set()
        if not addresses or not all(_private_ip(addr) for addr in addresses):
            raise HTTPException(422, "HTTP is allowed only for local or private-network providers.")
        local = True
    # The container listens on 8000 and the default host mapping is 8080.
    blocked = {8000}
    if not os.environ.get("APP_PORT"):
        blocked.add(8080)
    for variable in ("APP_PORT", "PORT"):
        val = os.environ.get(variable, "")
        if val.isdecimal() and 0 < int(val) <= 65535:
            blocked.add(int(val))
    effective_port = port if port is not None else (443 if parts.scheme == "https" else 80)
    if local and effective_port in blocked:
        raise HTTPException(422, "Provider URL points to a blocked application port.")
    if not local and not parsed_ip and effective_port in blocked:
        # Also block resolvable aliases of the app host at its port. This is
        # not sufficient against DNS rebinding or unknown reverse proxies.
        try:
            addresses = {ipaddress.ip_address(item[4][0]) for item in socket.getaddrinfo(host, effective_port, type=socket.SOCK_STREAM)}
        except (OSError, ValueError):
            addresses = set()
        if any(_private_ip(addr) or addr.is_link_local for addr in addresses):
            raise HTTPException(422, "Provider URL points to a blocked application port.")
    return value.rstrip("/")


async def _json_object(request: Request) -> dict[str, Any]:
    try:
        body = await request.body()
        if len(body) > _MAX_BODY:
            raise HTTPException(413, "Request is too large.")
        result = json.loads(body)
    except HTTPException:
        raise
    except (ValueError, UnicodeError):
        # Do not reflect invalid input: it may contain a provider API key.
        raise HTTPException(422, "Invalid JSON body.") from None
    if not isinstance(result, dict):
        raise HTTPException(422, "JSON body must be an object.")
    return result


def _text_field(value: Any, label: str, maximum: int, allow_empty: bool = True) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise HTTPException(422, f"Invalid {label}.")
    value = value.strip()
    if (not allow_empty and not value) or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise HTTPException(422, f"Invalid {label}.")
    return value


@router.get("")
def list_providers() -> dict[str, Any]:
    conn, _ = _open_store()
    try:
        rows = conn.execute("SELECT * FROM providers ORDER BY rowid").fetchall()
        active = conn.execute("SELECT value FROM settings WHERE key='active_provider'").fetchone()
        return {"providers": [_provider(row) for row in rows], "active_provider": active[0] if active else None}
    finally:
        conn.close()


@router.put("/active/select")
async def select_provider(request: Request) -> dict[str, Any]:
    body = await _json_object(request)
    if set(body) != {"id"} or (body["id"] is not None and not isinstance(body["id"], str)):
        raise HTTPException(422, "Provide a provider ID or null.")
    selected = body["id"]
    if selected is not None:
        _valid_id(selected)
    conn, _ = _open_store()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if selected is not None:
            row = conn.execute("SELECT enabled FROM providers WHERE id=?", (selected,)).fetchone()
            if not row:
                raise HTTPException(404, "Provider not found.")
            if not row["enabled"]:
                raise HTTPException(400, "Provider is disabled.")
        conn.execute("INSERT INTO settings(key,value) VALUES('active_provider',?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (selected,))
        conn.commit()
        return {"active_provider": selected}
    finally:
        conn.close()


@router.put("/{provider_id}")
async def put_provider(provider_id: str, request: Request) -> dict[str, Any]:
    _valid_id(provider_id)
    body = await _json_object(request)
    allowed = {"name", "kind", "base_url", "model", "api_key", "clear_key", "enabled"}
    if set(body) - allowed:
        raise HTTPException(422, "Unsupported provider setting.")
    changes: dict[str, Any] = {}
    for field, limit in (("name", 120), ("model", 200)):
        if field in body and body[field] is not None:
            changes[field] = _text_field(body[field], field, limit, allow_empty=(field == "model"))
    if "kind" in body and body["kind"] is not None:
        kind = _text_field(body["kind"], "kind", 32, allow_empty=False).lower()
        if kind not in _KIND_ALIASES:
            raise HTTPException(422, "Unsupported provider kind.")
        changes["kind"] = _KIND_ALIASES[kind]
    if "base_url" in body and body["base_url"] is not None:
        changes["base_url"] = _validate_url(_text_field(body["base_url"], "base URL", 2048))
    for field in ("enabled", "clear_key"):
        if field in body and not isinstance(body[field], bool):
            raise HTTPException(422, f"Invalid {field}.")
    if "enabled" in body:
        changes["enabled"] = int(body["enabled"])
    api_key = body.get("api_key")
    if api_key is not None:
        api_key = _text_field(api_key, "API key", 4096)
    if body.get("clear_key") and api_key:
        raise HTTPException(422, "Cannot set and clear the API key together.")

    conn, cipher = _open_store()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM providers WHERE id=?", (provider_id,)).fetchone()
        if row:
            values = dict(row)
        else:
            values = {"id": provider_id, "name": provider_id, "kind": "openai",
                      "base_url": "", "model": "", "enabled": 1, "encrypted_key": None}
        values.update(changes)
        if row and row["encrypted_key"] and not body.get("clear_key") and not api_key:
            try:
                old_origin = _url_origin(row["base_url"])
                new_origin = _url_origin(values["base_url"])
            except ValueError:
                raise HTTPException(409, "Cannot verify the provider URL; replace or clear the saved API key.") from None
            if row["kind"] != values["kind"] or old_origin != new_origin:
                raise HTTPException(409, "Changing provider kind or URL origin requires replacing or clearing the saved API key.")
        if body.get("clear_key"):
            values["encrypted_key"] = None
        elif api_key:
            values["encrypted_key"] = cipher.encrypt(api_key.encode("utf-8"))
        conn.execute("""INSERT INTO providers (id,name,kind,base_url,model,enabled,encrypted_key)
                        VALUES (:id,:name,:kind,:base_url,:model,:enabled,:encrypted_key)
                        ON CONFLICT(id) DO UPDATE SET name=excluded.name,kind=excluded.kind,
                        base_url=excluded.base_url,model=excluded.model,enabled=excluded.enabled,
                        encrypted_key=excluded.encrypted_key""", values)
        if not values["enabled"]:
            conn.execute("UPDATE settings SET value=NULL WHERE key='active_provider' AND value=?", (provider_id,))
        conn.commit()
        return _provider(conn.execute("SELECT * FROM providers WHERE id=?", (provider_id,)).fetchone())
    finally:
        conn.close()


def _load_provider(provider_id: str) -> tuple[dict[str, Any], str | None]:
    _valid_id(provider_id)
    conn, cipher = _open_store()
    try:
        row = conn.execute("SELECT * FROM providers WHERE id=?", (provider_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Provider not found.")
        config = _provider(row)
        encrypted_key = row["encrypted_key"]
        if not encrypted_key:
            return config, None
        try:
            key = cipher.decrypt(encrypted_key).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError, TypeError, ValueError):
            raise HTTPException(503, "Stored provider key cannot be decrypted; check encryption key configuration.") from None
        return config, key
    finally:
        conn.close()


def _http_client() -> httpx.AsyncClient:
    # No redirects (or environment proxy) to avoid forwarding secret headers.
    return httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=5.0, write=10.0, pool=5.0),
                             follow_redirects=False, trust_env=False)


def _endpoint(base_url: str, kind: str, model: str) -> str:
    parts = urlsplit(base_url)
    base = base_url.rstrip("/")
    if kind == "google":
        if not parts.path.strip("/"):
            base += "/v1beta"
        model = model.removeprefix("models/")
        return f"{base}/models/{quote(model, safe='')}:generateContent"
    if not parts.path.strip("/"):
        base += "/v1"
    return base + ("/messages" if kind == "anthropic" else "/chat/completions")


def _extract_text(kind: str, result: Any) -> str:
    try:
        if kind == "google":
            blocks = result["candidates"][0]["content"]["parts"]
            return "\n".join(part["text"] for part in blocks if isinstance(part.get("text"), str)).strip()
        if kind == "anthropic":
            return "\n".join(part["text"] for part in result["content"] if part.get("type") == "text" and isinstance(part.get("text"), str)).strip()
        content = result["choices"][0]["message"]["content"]
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            return "\n".join(part["text"] for part in content if isinstance(part, dict) and isinstance(part.get("text"), str)).strip()
    except (KeyError, IndexError, TypeError, AttributeError):
        pass
    return ""


async def _complete(config: dict[str, Any], key: str | None, prompt: str, max_tokens: int) -> tuple[str | None, str | None, int | None]:
    if not config["base_url"] or not config["model"]:
        return None, "Configure a base URL and model first.", None
    if config["kind"] in ("google", "anthropic") and not key:
        return None, "Configure an API key first.", None
    try:
        base_url = _validate_url(config["base_url"])
    except HTTPException:
        return None, "Provider base URL is invalid or not allowed.", None
    kind = config["kind"]
    headers = {"Accept": "application/json"}
    if kind == "google":
        headers["x-goog-api-key"] = key or ""
        payload = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                   "generationConfig": {"maxOutputTokens": max_tokens}}
    elif kind == "anthropic":
        headers.update({"x-api-key": key or "", "anthropic-version": "2023-06-01"})
        payload = {"model": config["model"], "max_tokens": max_tokens,
                   "messages": [{"role": "user", "content": prompt}]}
    else:
        if key:
            headers["Authorization"] = "Bearer " + key
        payload = {"model": config["model"], "max_tokens": max_tokens,
                   "messages": [{"role": "user", "content": prompt}]}
    try:
        async with _http_client() as client:
            response = await client.post(_endpoint(base_url, kind, config["model"]), headers=headers, json=payload)
        if not 200 <= response.status_code < 300:
            return None, f"Provider returned HTTP {response.status_code}.", response.status_code
        text = _extract_text(kind, response.json())
        if not text:
            return None, "Provider returned no readable completion.", response.status_code
        # A hostile or misconfigured provider could echo the authentication header.
        if key:
            text = text.replace(key, "[REDACTED]")
        return text, None, response.status_code
    except httpx.TimeoutException:
        return None, "Provider request timed out.", None
    except (httpx.HTTPError, OSError):
        return None, "Could not connect to the provider.", None
    except (ValueError, TypeError):
        return None, "Provider returned an invalid response.", None
    except Exception:
        # Never propagate or log client/provider exception text, which can
        # contain URLs, headers, tokens or upstream response bodies.
        return None, "Provider request failed.", None


@router.post("/{provider_id}/test")
async def test_provider(provider_id: str) -> dict[str, Any]:
    config, key = _load_provider(provider_id)
    # Some reasoning models consume output tokens before emitting visible text.
    text, error, status = await _complete(config, key, "Reply with OK.", 256)
    result: dict[str, Any] = {"ok": error is None, "message": "Provider completion succeeded." if text is not None else error}
    if status is not None:
        result["status_code"] = status
    return result


@router.post("/{provider_id}/analyze")
async def analyze_provider(provider_id: str, request: Request) -> dict[str, Any]:
    body = await _json_object(request)
    if set(body) != {"context"} or not isinstance(body.get("context"), str):
        raise HTTPException(422, "Provide a context string.")
    context = body["context"]
    if not context.strip() or len(context) > 50_000:
        raise HTTPException(422, "Context must contain 1 to 50000 characters.")
    config, key = _load_provider(provider_id)
    if not config["enabled"]:
        return {"ok": False, "analysis": "", "message": "Provider is disabled."}
    prompt = ("Provide an educational analysis of the following context. State uncertainties and risks; "
              "do not claim to place trades or execute actions. This is not investment advice.\n\n" + context)
    text, error, status = await _complete(config, key, prompt, 700)
    result: dict[str, Any] = {"ok": error is None, "analysis": text or ""}
    if error:
        result["message"] = error
    if status is not None:
        result["status_code"] = status
    return result
