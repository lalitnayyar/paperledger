"""Independent market-data credentials, encrypted using the LLM store cipher."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import logging
import sqlite3

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from app import llm

router = APIRouter(prefix="/api/market-data", tags=["market-data"])


class _HideQuoteRequests(logging.Filter):
    """httpx logs complete request URLs at INFO, including query-string keys."""
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage().lower()
        return "alphavantage.co" not in message and "finnhub.io" not in message


# Do not alter global log levels; suppress market provider request log lines.
_httpx_logger = logging.getLogger("httpx")
if not any(isinstance(f, _HideQuoteRequests) for f in _httpx_logger.filters):
    _httpx_logger.addFilter(_HideQuoteRequests())


# httpcore's DEBUG trace may include response headers or exception details
# without mentioning the host. Limit suppression to this request's context;
# concurrent unrelated requests keep their ordinary logs.
_private_finnhub_request = ContextVar("private_finnhub_request", default=False)


class _HideFinnhubTransport(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _private_finnhub_request.get()


for _logger_name in ("httpcore.connection", "httpcore.http11", "httpcore.http2", "httpcore.proxy", "httpcore.socks"):
    _logger = logging.getLogger(_logger_name)
    if not any(isinstance(f, _HideFinnhubTransport) for f in _logger.filters):
        _logger.addFilter(_HideFinnhubTransport())


@contextmanager
def private_finnhub_request():
    token = _private_finnhub_request.set(True)
    try:
        yield
    finally:
        _private_finnhub_request.reset(token)


class MarketDataUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    api_key: str | None = None
    clear_key: StrictBool = False

    @field_validator("api_key")
    @classmethod
    def valid_key(cls, value):
        if value is None:
            return value
        value = value.strip()
        if not value or len(value) > 4096 or any(ord(c) < 33 or ord(c) == 127 for c in value):
            raise ValueError("Invalid API key")
        if value.lower() == "demo":
            raise ValueError("The demo key is used automatically for IBM only; configure your own key instead")
        return value

    @model_validator(mode="after")
    def valid_operation(self):
        if self.clear_key and "api_key" in self.model_fields_set:
            raise ValueError("Cannot set and clear the API key together")
        if self.api_key is None and not self.clear_key:
            raise ValueError("Provide an API key or request key removal")
        return self


class FinnhubUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    api_key: str | None = Field(default=None, strict=True, repr=False)
    clear_key: StrictBool = False

    @field_validator("api_key")
    @classmethod
    def valid_key(cls, value):
        if value is None:
            return value
        value = value.strip()
        if not value or len(value) > 4096 or not value.isascii() or any(ord(c) < 33 or ord(c) > 126 for c in value):
            raise ValueError("Invalid API key")
        return value

    @model_validator(mode="after")
    def valid_operation(self):
        if self.clear_key and "api_key" in self.model_fields_set:
            raise ValueError("Cannot set and clear the API key together")
        if self.api_key is None and not self.clear_key:
            raise ValueError("Provide an API key or request key removal")
        return self


def _open_market_store():
    conn, cipher = llm._open_store()
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS market_credentials (id INTEGER PRIMARY KEY CHECK(id=1), encrypted_key BLOB NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS finnhub_market_credentials (id INTEGER PRIMARY KEY CHECK(id=1), encrypted_key BLOB NOT NULL)")
        conn.commit()
        return conn, cipher
    except sqlite3.Error:
        conn.close()
        raise HTTPException(503, "Market credential storage is unavailable.") from None


def load_key() -> str | None:
    conn, cipher = _open_market_store()
    try:
        row = conn.execute("SELECT encrypted_key FROM market_credentials WHERE id=1").fetchone()
        if row is None:
            return None
        try:
            key = cipher.decrypt(row[0]).decode("utf-8")
            if not key:
                raise ValueError("empty key")
            return key
        except (InvalidToken, UnicodeDecodeError, TypeError, ValueError):
            raise HTTPException(503, "Stored market data key cannot be decrypted; check encryption key configuration.") from None
    except sqlite3.Error:
        raise HTTPException(503, "Market credential storage is unavailable.") from None
    finally:
        conn.close()


def _status(key: str | None) -> dict:
    return {"stock_provider": "Alpha Vantage", "has_api_key": key is not None, "using_demo": key is None}


def load_finnhub_key() -> str | None:
    conn, cipher = _open_market_store()
    try:
        row = conn.execute("SELECT encrypted_key FROM finnhub_market_credentials WHERE id=1").fetchone()
        if row is None:
            return None
        try:
            key = cipher.decrypt(row[0]).decode("utf-8")
            if not key:
                raise ValueError("empty key")
            return key
        except (InvalidToken, UnicodeDecodeError, TypeError, ValueError):
            raise HTTPException(503, "Stored Finnhub key cannot be decrypted; check encryption key configuration.") from None
    except sqlite3.Error:
        raise HTTPException(503, "Market credential storage is unavailable.") from None
    finally:
        conn.close()


@router.get("/finnhub")
def get_finnhub_market_data():
    return {"has_api_key": load_finnhub_key() is not None}


@router.put("/finnhub")
def put_finnhub_market_data(payload: FinnhubUpdate):
    conn, cipher = _open_market_store()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if payload.clear_key:
            conn.execute("DELETE FROM finnhub_market_credentials WHERE id=1")
        else:
            encrypted = cipher.encrypt(payload.api_key.encode("ascii"))
            conn.execute("INSERT INTO finnhub_market_credentials(id,encrypted_key) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET encrypted_key=excluded.encrypted_key", (encrypted,))
        conn.commit()
        return {"has_api_key": not payload.clear_key}
    except sqlite3.Error:
        conn.rollback()
        raise HTTPException(503, "Market credential storage is unavailable.") from None
    finally:
        conn.close()


@router.get("")
def get_market_data():
    return _status(load_key())


@router.put("")
def put_market_data(payload: MarketDataUpdate):
    conn, cipher = _open_market_store()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if payload.clear_key:
            conn.execute("DELETE FROM market_credentials WHERE id=1")
        else:
            encrypted = cipher.encrypt(payload.api_key.encode("utf-8"))
            conn.execute("INSERT INTO market_credentials(id,encrypted_key) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET encrypted_key=excluded.encrypted_key", (encrypted,))
        conn.commit()
        return _status(None if payload.clear_key else payload.api_key)
    except sqlite3.Error:
        conn.rollback()
        raise HTTPException(503, "Market credential storage is unavailable.") from None
    finally:
        conn.close()
