"""Alpha Vantage credential management, separate from LLM provider credentials."""
from __future__ import annotations

import logging
import sqlite3

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, StrictBool, field_validator, model_validator

from app import llm

router = APIRouter(prefix="/api/market-data", tags=["market-data"])


class _HideQuoteRequests(logging.Filter):
    """httpx logs complete request URLs at INFO, including query-string keys."""
    def filter(self, record: logging.LogRecord) -> bool:
        return "alphavantage.co" not in record.getMessage().lower()


# Do not alter global log levels; suppress only Alpha Vantage request log lines.
_httpx_logger = logging.getLogger("httpx")
if not any(isinstance(f, _HideQuoteRequests) for f in _httpx_logger.filters):
    _httpx_logger.addFilter(_HideQuoteRequests())


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


def _open_market_store():
    conn, cipher = llm._open_store()
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS market_credentials (id INTEGER PRIMARY KEY CHECK(id=1), encrypted_key BLOB NOT NULL)")
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
