"""Offline watchlist imports: no live Yahoo requests and no credentials."""
import csv
import io
import socket
from decimal import Decimal

import pytest

from app import db, trading, yahoo_quotes


def csv_text(headers, *rows):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(headers)
    writer.writerows(rows)
    return stream.getvalue()


def test_yahoo_watchlist_import_matches_candidate_contract(monkeypatch):
    monkeypatch.setattr(db, "utc_now", lambda: "2026-11-08T12:34:56.000000Z")
    data = csv_text(
        ["Symbol", "Current Price", "Date", "Volume", "Currency", "Notes"],
        [" brk.b ", "$1,234.50", "11/06/2026", "1,234,567", "USD", "untrusted,ignored"],
    )
    items, warnings = yahoo_quotes.import_quotes(data)
    assert len(items) == 1
    item = items[0]
    assert set(item) == set(trading._CANDIDATE_FIELDS)
    assert item == {
        "symbol": "BRK.B", "chain": None, "pair_address": None,
        "price_usd": "1234.50", "liquidity_usd": None, "volume_24h": None,
        "price_change_24h": None, "volume_spike_ratio": None, "pair_created_at": None,
        "risk_status": "manual only", "reason": item["reason"], "url": None,
        "observed_at": "2026-11-08T12:34:56.000000Z", "asset_type": "stock",
        "source": "User import (Yahoo Finance)", "quote_date": "2026-11-06",
        "stock_volume_shares": 1234567, "stock_change_percent": None, "cache_status": "manual",
    }
    assert all(word in item["reason"].lower() for word in ("user-imported", "assumed", "stale", "manual", "not independently verified"))
    assert "not verified" in warnings[0] and "stale" in warnings[0]
    assert "untrusted" not in str(items) + str(warnings)


@pytest.mark.parametrize("symbol_header,price_header", [
    ("Symbol", "Price"), ("Ticker", "Current Price"), ("Ticker Symbol", "Last Price"),
    ("symbol", "Last"), ("SYMBOL", "price_usd"), ("symbol", "CURRENT-PRICE"),
])
def test_common_headers_bom_blank_lines_and_optional_columns(symbol_header, price_header):
    text = "\ufeff\n" + csv_text([symbol_header, price_header], ["aapl", "185.25"])
    items, warnings = yahoo_quotes.import_quotes(text)
    assert len(items) == 1
    assert items[0]["symbol"] == "AAPL" and items[0]["price_usd"] == "185.25"
    assert items[0]["quote_date"] is None
    assert items[0]["stock_volume_shares"] is None
    assert len(warnings) == 1
    byte_items, _ = yahoo_quotes.import_quotes(text.encode("utf-8"))
    assert byte_items[0]["symbol"] == "AAPL"


def test_standard_csv_optional_null_and_zero_volume():
    data = csv_text(["symbol", "price", "date", "volume"],
                    ["AAPL", "100", "", "N/A"], ["BF-B", "1.5", "2024-02-29", "0"])
    items, warnings = yahoo_quotes.import_quotes(data)
    assert [x["symbol"] for x in items] == ["AAPL", "BF-B"]
    assert items[0]["quote_date"] is None and items[0]["stock_volume_shares"] is None
    assert items[1]["quote_date"] == "2024-02-29" and items[1]["stock_volume_shares"] == 0
    assert len(warnings) == 1


def test_quoted_newline_and_ignored_fields_do_not_escape():
    injected = '=HYPERLINK("https://evil.example/private","open")\r\nBAD'
    data = csv_text(["Symbol", "Last Price", "Other"], ["IBM", "99", injected])
    items, warnings = yahoo_quotes.import_quotes(data)
    assert len(items) == 1
    assert injected not in str(items) + str(warnings)
    assert "evil.example" not in str(items) + str(warnings)


@pytest.mark.parametrize("symbol", [
    "", "$IBM", "IBM/USD", "NYSE:IBM", "123", "BRK..B", "TOOLONGTICKER", "A.BBBBZ",
    "ß", "İ", "ＡＡＰＬ", "=HYPERLINK(\"https://secret.invalid\")", "AAPL\nINJECTION", "AAA,BBB",
])
def test_bad_symbol_is_rejected_and_not_echoed(symbol):
    items, warnings = yahoo_quotes.import_quotes(csv_text(["symbol", "price"], [symbol, "1"]))
    assert not items
    assert "Row 2" in warnings[-1] and "ticker" in warnings[-1]
    if symbol:
        assert symbol not in " ".join(warnings)


@pytest.mark.parametrize("price", [
    "0", "-1", "nan", "NaN", "Infinity", "-inf", "1e309", "1e999999999999999999999999",
    "1000000000000000001", "0.0000000000000000001", "1,23", "1,234,56", "=1+2",
    "+1+2", "123xyz", "1\n2", "", "-", "$", "1_000",
])
def test_invalid_price_does_not_produce_candidate(price):
    items, warnings = yahoo_quotes.import_quotes(csv_text(["symbol", "price"], ["IBM", price]))
    assert items == [] and "invalid or non-positive price" in warnings[-1]


@pytest.mark.parametrize("value", [float("inf"), float("nan"), True, Decimal("NaN"), pytest.param(10**5000, id="huge-int"), None])
def test_bad_structured_price_values(value):
    items, warnings = yahoo_quotes.import_quotes([{"symbol": "IBM", "price": value}])
    assert items == [] and "Row 1" in warnings[-1]


@pytest.mark.parametrize("volume", ["-1", "1.2", "NaN", "1e19", "12,34", "=1+2", "$4"])
def test_bad_supplied_volume_rejects_row(volume):
    items, warnings = yahoo_quotes.import_quotes(csv_text(["symbol", "price", "volume"], ["IBM", "2", volume]))
    assert not items and "invalid share volume" in warnings[-1]


def test_fractional_decimal_integral_volume_and_scientific_price_are_bounded():
    items, warnings = yahoo_quotes.import_quotes([{"symbol": "IBM", "price": "1.25e2", "volume": "100.0"}])
    assert len(items) == 1 and Decimal(items[0]["price_usd"]) == Decimal("125")
    assert items[0]["stock_volume_shares"] == 100 and len(warnings) == 1


@pytest.mark.parametrize("day", ["2025-02-29", "2026-13-01", "13/01/2026", "2026-11-06T10:00:00Z", "tomorrow"])
def test_invalid_date_rejects_row_without_inventing_date(day):
    items, warnings = yahoo_quotes.import_quotes(csv_text(["symbol", "price", "date"], ["IBM", "2", day]))
    assert not items and "invalid date" in warnings[-1]
    assert day not in warnings[-1]


@pytest.mark.parametrize("currency", ["EUR", "CAD", "USDT", "$", "=USD", "usd\n"])
def test_explicit_currency_must_be_usd(currency):
    # Whitespace trimming is permitted; use interior newline to test unsafe content.
    if currency == "usd\n":
        currency = "U\nSD"
    items, warnings = yahoo_quotes.import_quotes(csv_text(["symbol", "price", "currency"], ["IBM", "2", currency]))
    assert not items and "currency must be USD" in warnings[-1]


def test_structured_rows_aliases_duplicate_and_partial_results():
    rows = [
        {"Ticker": "bad/symbol", "Last": "100"},
        {"Ticker": "ibm", "Last Price": 2, "Quote Date": "6/4/2026", "Volume Shares": 10, "Currency": "usd"},
        {"symbol": " IBM ", "price": 9},
        {"symbol": "AAPL", "price": 3, "Last": 4},
        {"symbol": "MSFT", "price": "3", "date": "N/A", "volume": "--"},
        "not a row",
    ]
    items, warnings = yahoo_quotes.import_quotes(rows)
    assert [item["symbol"] for item in items] == ["IBM", "MSFT"]
    assert items[0]["quote_date"] == "2026-06-04" and items[0]["stock_volume_shares"] == 10
    assert items[1]["quote_date"] is None and items[1]["stock_volume_shares"] is None
    assert all(any(f"Row {i}:" in warning for warning in warnings) for i in (1, 3, 4, 6))
    assert "bad/symbol" not in str(warnings) and "not a row" not in str(warnings)


@pytest.mark.parametrize("data,expected", [
    ("", "empty"), ("symbol,price\n", "No data"), ("date,close\n2026-01-01,10", "requires"),
    ("symbol,ticker,price\nIBM,IBM,1", "duplicate"), ("symbol,price,Last\nIBM,1,2", "duplicate"),
    ('symbol,price\nIBM,"1', "malformed"), (b"symbol,price\nIBM,\xff", "UTF-8"),
    ("symbol,price\nIBM," + "x" * yahoo_quotes.MAX_IMPORT_BYTES, "size limit"),
    (None, "must be CSV"), ({"symbol": "IBM"}, "must be CSV"),
])
def test_invalid_document_is_sanitized(data, expected):
    items, warnings = yahoo_quotes.import_quotes(data)
    assert not items and expected in warnings[-1]


def test_25_row_limit_no_silent_truncation():
    rows = [[chr(ord("A") + i), "1"] for i in range(26)]
    items, warnings = yahoo_quotes.import_quotes(csv_text(["symbol", "price"], *rows[:25]))
    assert len(items) == 25 and len(warnings) == 1
    items, warnings = yahoo_quotes.import_quotes(csv_text(["symbol", "price"], *rows))
    assert not items and "more than 25" in warnings[-1]
    items, warnings = yahoo_quotes.import_quotes([{"symbol": row[0], "price": row[1]} for row in rows])
    assert not items and "more than 25" in warnings[-1]


def test_unquoted_thousands_separator_not_misread_and_bad_rows_skipped():
    items, warnings = yahoo_quotes.import_quotes("symbol,price,volume\nIBM,1,234,100\nAAPL,2,42\n")
    assert [item["symbol"] for item in items] == ["AAPL"]
    assert "incorrect number of columns" in warnings[-1]


def test_field_and_column_limits_and_structured_nested_values():
    csv_data = csv_text(["symbol", "price", "x"], ["IBM", "1", "x" * 1025], ["AAPL", "2", "ok"])
    items, warnings = yahoo_quotes.import_quotes(csv_data)
    assert [item["symbol"] for item in items] == ["AAPL"]
    assert "field exceeds" in warnings[-1]
    items, warnings = yahoo_quotes.import_quotes(csv_text(["symbol", "price"] + [f"x{i}" for i in range(63)], ["IBM", "1"] + [""] * 63))
    assert not items and "Too many columns" in warnings[-1]
    items, warnings = yahoo_quotes.import_quotes([{"symbol": "IBM", "price": 1, "unknown": {"secret": "do-not-expose"}}])
    assert not items and "invalid field value" in warnings[-1]
    assert "do-not-expose" not in str(warnings)


def test_structured_size_limit_is_global():
    rows = [{"symbol": "IBM", "price": "1", **{f"field{i}": "x" * 1000 for i in range(6)}} for _ in range(25)]
    items, warnings = yahoo_quotes.import_quotes(rows)
    assert not items and "size limit" in warnings[-1]


def test_import_is_strictly_offline(monkeypatch):
    def no_socket(*args, **kwargs):
        raise AssertionError("network use is forbidden")
    monkeypatch.setattr(socket, "socket", no_socket)
    items, warnings = yahoo_quotes.import_quotes("Symbol,Current Price\nIBM,12.5\n")
    assert len(items) == 1 and len(warnings) == 1
