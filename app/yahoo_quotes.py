"""Import user-provided Yahoo Finance watchlist data; never contact Yahoo.

Yahoo's terms restrict automated collection without express prior permission.
Users are responsible for obtaining and using their data in accordance with
applicable terms. This module has no network client and does not verify the
source, listing, currency, age, completeness, or accuracy of imported data.

``import_quotes`` accepts a UTF-8 CSV string/bytes or a list of row dictionaries.
The CSV needs a Symbol/Ticker and a Price/Current Price/Last Price/Last column;
Date, Volume and Currency are optional. Prices are assumed USD only when no
currency is supplied; an explicit non-USD currency is rejected. Dates are
user-supplied dates, not verified quote times or necessarily trading days.
All input is bounded, and invalid rows are skipped with sanitized warnings.
There is no fallback, fetching, caching, automatic trading or position marking.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app import db

MAX_ROWS = 25
MAX_IMPORT_BYTES = 128 * 1024
MAX_COLUMNS = 64
MAX_FIELD_CHARS = 1024
MAX_HEADER_CHARS = 128

_SYMBOL = re.compile(r"[A-Z]{1,5}(?:[.-][A-Z]{1,4})?", re.ASCII)
_NUMBER = re.compile(
    r"[+-]?(?:(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?",
    re.ASCII,
)
_LIMIT = Decimal("1e18")
_MISSING = {"", "-", "--", "N/A", "NA", "NULL"}
_ALIASES = {
    "symbol": "symbol", "ticker": "symbol", "tickersymbol": "symbol",
    "price": "price", "priceusd": "price", "currentprice": "price",
    "lastprice": "price", "last": "price",
    "date": "date", "quotedate": "date", "tradedate": "date",
    "lasttradedate": "date",
    "volume": "volume", "stockvolumeshares": "volume", "volumeshares": "volume",
    "currency": "currency", "currencycode": "currency",
}
_DISCLOSURE = (
    "Yahoo Finance data is manually imported; its origin, US listing, USD currency, "
    "quote date and freshness are not verified. It may be stale."
)
_REASON = (
    "User-imported Yahoo Finance data, not independently verified; US listing and "
    "USD are assumed. Date, if provided, is user-supplied. The price and volume "
    "may be stale or incomplete. Manual paper trading only; no automatic buy, "
    "position mark, liquidity check, or risk assessment."
)


class _InputError(Exception):
    """A fixed, non-sensitive message safe to return to a user."""


def _header(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > MAX_HEADER_CHARS:
        return None
    if any(not c.isprintable() for c in value):
        return None
    return _ALIASES.get(re.sub(r"[ _-]+", "", value.strip().lower()))


def _column_mapping(headers: list[Any]) -> dict[str, int]:
    if len(headers) > MAX_COLUMNS:
        raise _InputError("Too many columns in the import.")
    mapping: dict[str, int] = {}
    for index, header in enumerate(headers):
        if not isinstance(header, str) or len(header) > MAX_HEADER_CHARS or any(not c.isprintable() for c in header):
            raise _InputError("Invalid column header in the import.")
        canonical = _header(header)
        if canonical is not None:
            if canonical in mapping:
                raise _InputError("Ambiguous duplicate import columns.")
            mapping[canonical] = index
    if "symbol" not in mapping or "price" not in mapping:
        raise _InputError("Import requires a Symbol/Ticker and a Price/Current Price/Last Price/Last column.")
    return mapping


def _csv_rows(data: str | bytes) -> list[tuple[int, dict[str, Any]]]:
    if isinstance(data, bytes):
        if len(data) > MAX_IMPORT_BYTES:
            raise _InputError("Import exceeds the size limit.")
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise _InputError("CSV must be valid UTF-8.") from None
    else:
        if len(data) > MAX_IMPORT_BYTES:
            raise _InputError("Import exceeds the size limit.")
        try:
            if len(data.encode("utf-8")) > MAX_IMPORT_BYTES:
                raise _InputError("Import exceeds the size limit.")
        except UnicodeEncodeError:
            raise _InputError("CSV must be valid UTF-8.") from None
        text = data.removeprefix("\ufeff")
    try:
        reader = csv.reader(io.StringIO(text, newline=""), strict=True)
        records: list[list[str]] = []
        for row in reader:
            if not row or all(not field.strip() for field in row):
                continue
            records.append(row)
            if len(records) > MAX_ROWS + 1:
                raise _InputError("Import contains more than 25 data rows; nothing was imported.")
    except csv.Error:
        raise _InputError("CSV is malformed; nothing was imported.") from None
    if not records:
        raise _InputError("CSV is empty; a header and data rows are required.")
    headers = records[0]
    mapping = _column_mapping(headers)
    rows: list[tuple[int, dict[str, Any]]] = []
    for number, row in enumerate(records[1:], 2):
        # Sentinel values produce row-specific warnings without echoing input.
        if len(row) != len(headers):
            rows.append((number, {"__error__": "incorrect number of columns"}))
        elif any(len(value) > MAX_FIELD_CHARS for value in row):
            rows.append((number, {"__error__": "field exceeds the size limit"}))
        else:
            rows.append((number, {name: row[index] for name, index in mapping.items()}))
    return rows


def _structured_rows(data: list[Any]) -> list[tuple[int, dict[str, Any]]]:
    if len(data) > MAX_ROWS:
        raise _InputError("Import contains more than 25 data rows; nothing was imported.")
    rows: list[tuple[int, dict[str, Any]]] = []
    total_bytes = 0
    for number, row in enumerate(data, 1):
        if not isinstance(row, dict):
            rows.append((number, {"__error__": "expected a row object"}))
            continue
        if len(row) > MAX_COLUMNS:
            rows.append((number, {"__error__": "too many columns"}))
            continue
        try:
            fields: dict[str, Any] = {}
            for key, value in row.items():
                if not isinstance(key, str) or len(key) > MAX_HEADER_CHARS or any(not c.isprintable() for c in key):
                    raise _InputError("invalid column header")
                if isinstance(value, str):
                    if len(value) > MAX_FIELD_CHARS:
                        raise _InputError("field exceeds the size limit")
                    total_bytes += len(value.encode("utf-8"))
                elif value is None or isinstance(value, (int, float, Decimal, bool)):
                    # Huge integers need not be converted to strings to check their size.
                    if isinstance(value, int) and value.bit_length() > 4096:
                        raise _InputError("invalid field value")
                    total_bytes += len(str(value))
                else:
                    raise _InputError("invalid field value")
                total_bytes += len(key.encode("utf-8"))
                if total_bytes > MAX_IMPORT_BYTES:
                    raise _InputError("Import exceeds the size limit.")
                canonical = _header(key)
                if canonical is not None:
                    if canonical in fields:
                        raise _InputError("ambiguous duplicate columns")
                    fields[canonical] = value
            if "symbol" not in fields or "price" not in fields:
                raise _InputError("symbol and price are required")
            rows.append((number, fields))
        except UnicodeEncodeError:
            rows.append((number, {"__error__": "invalid text encoding"}))
        except _InputError as exc:
            if str(exc) == "Import exceeds the size limit.":
                raise
            rows.append((number, {"__error__": str(exc)}))
    return rows


def _missing(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip().upper() in _MISSING)


def _number(value: Any, *, dollar: bool = False) -> Decimal | None:
    if value is None or isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    if isinstance(value, int) and value.bit_length() > 4096:
        return None
    text = str(value).strip()
    if dollar and text.startswith("$"):
        text = text[1:].strip()
    if len(text) > 128 or not _NUMBER.fullmatch(text):
        return None
    try:
        result = Decimal(text.replace(",", ""))
        if not result.is_finite() or result.copy_abs() > _LIMIT or result.as_tuple().exponent < -18:
            return None
        return result
    except (InvalidOperation, ValueError, OverflowError):
        return None


def _date(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    try:
        if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            return date.fromisoformat(value).isoformat()
        if re.fullmatch(r"[0-9]{1,2}/[0-9]{1,2}/[0-9]{4}", value):
            return datetime.strptime(value, "%m/%d/%Y").date().isoformat()
    except ValueError:
        pass
    return None


def _candidate(row: dict[str, Any], observed: str) -> tuple[dict[str, Any] | None, str | None]:
    symbol = row.get("symbol")
    if not isinstance(symbol, str) or not symbol.isascii():
        return None, "invalid US ticker format"
    symbol = symbol.strip().upper()
    if not _SYMBOL.fullmatch(symbol):
        return None, "invalid US ticker format"
    currency = row.get("currency")
    if not _missing(currency) and (not isinstance(currency, str) or currency.strip().upper() != "USD"):
        return None, "currency must be USD or unspecified"
    price = _number(row.get("price"), dollar=True)
    if price is None or price <= 0:
        return None, "invalid or non-positive price"
    raw_date = row.get("date")
    quote_date = None if _missing(raw_date) else _date(raw_date)
    if not _missing(raw_date) and quote_date is None:
        return None, "invalid date (use YYYY-MM-DD or MM/DD/YYYY)"
    raw_volume = row.get("volume")
    volume = None
    if not _missing(raw_volume):
        number = _number(raw_volume)
        if number is None or number < 0 or number != number.to_integral_value():
            return None, "invalid share volume"
        volume = int(number)
    return {
        "symbol": symbol, "chain": None, "pair_address": None,
        "price_usd": str(price), "liquidity_usd": None, "volume_24h": None,
        "price_change_24h": None, "volume_spike_ratio": None, "pair_created_at": None,
        "risk_status": "manual only", "reason": _REASON, "url": None,
        "observed_at": observed, "asset_type": "stock", "source": "User import (Yahoo Finance)",
        "quote_date": quote_date, "stock_volume_shares": volume,
        "stock_change_percent": None, "cache_status": "manual",
    }, None


def import_quotes(data: str | bytes | list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """Return (stock candidates, sanitized warnings) for a local CSV or row list.

    Maximum 25 nonblank data rows; if exceeded, reject the entire import. CSV
    row numbers are logical nonblank records starting at 2 (after the header),
    while structured row numbers start at 1. Duplicate symbols are skipped after
    their first valid row. ``observed_at`` is the import time, never a quote time.
    """
    warnings = [_DISCLOSURE]
    try:
        if isinstance(data, (str, bytes)):
            rows = _csv_rows(data)
        elif isinstance(data, list):
            rows = _structured_rows(data)
        else:
            raise _InputError("Import must be CSV text, UTF-8 CSV bytes, or a list of row objects.")
    except _InputError as exc:
        return [], warnings + [str(exc)]
    if not rows:
        return [], warnings + ["No data rows were supplied."]
    observed = db.utc_now()
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for number, row in rows:
        if "__error__" in row:
            warnings.append(f"Row {number}: {row['__error__']}; skipped.")
            continue
        candidate, error = _candidate(row, observed)
        if error is not None:
            warnings.append(f"Row {number}: {error}; skipped.")
        elif candidate is not None and candidate["symbol"] in seen:
            warnings.append(f"Row {number}: duplicate symbol; skipped.")
        elif candidate is not None:
            seen.add(candidate["symbol"])
            candidates.append(candidate)
    return candidates, warnings
