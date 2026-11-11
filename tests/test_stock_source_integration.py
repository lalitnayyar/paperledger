"""Stock-source integration: synthetic fixtures, a real API/SQLite, and no network.

The examples below are invented test prices, not market data.  CSV dates are
user-supplied dates, not proof of the time at which the quote was made.  The
Finnhub fixture's provider timestamp is deliberately much older than fetch time.
"""
import asyncio
import concurrent.futures
import json
import socket
import sqlite3
import threading
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db, trading
from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("APP_ENCRYPTION_KEY", raising=False)

    def no_dns(*args, **kwargs):
        raise AssertionError("Integration tests must not resolve real network hosts")

    monkeypatch.setattr(socket, "getaddrinfo", no_dns)
    monkeypatch.setattr(socket.socket, "connect", no_dns)
    monkeypatch.setattr(socket.socket, "connect_ex", no_dns)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def upstream(monkeypatch):
    """Record every attempted HTTP request; only explicitly mocked hosts respond."""
    class Feed:
        calls = []
        alpha = "quote"
        alpha_price = "181.25"  # synthetic, not a real quote
        finnhub_timestamp = 1700000000  # 2023-11-14 22:13:20 UTC, not fetch time

    def handler(request):
        Feed.calls.append(request)
        if request.url.host == "www.alphavantage.co":
            assert request.url.path == "/query"
            if Feed.alpha == "quota":
                return httpx.Response(200, json={"Note": "Synthetic test quota limit"})
            if Feed.alpha == "empty":
                return httpx.Response(200, json={"Global Quote": {}})
            symbol = request.url.params["symbol"]
            return httpx.Response(200, json={"Global Quote": {
                "01. symbol": symbol, "05. price": Feed.alpha_price,
                "06. volume": "1234", "07. latest trading day": "2020-01-02",
                "10. change percent": "1.25%",
            }})
        if request.url.host == "finnhub.io":
            assert request.url.path == "/api/v1/quote"
            return httpx.Response(200, json={"c": 202.5, "d": 2.5, "dp": 1.25,
                                             "h": 205, "l": 199, "o": 200,
                                             "pc": 200, "t": Feed.finnhub_timestamp})
        raise AssertionError(f"Unexpected upstream request to {request.url.host}")

    original_client = httpx.AsyncClient

    def mock_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_client(*args, **kwargs)

    # The scanner imports httpx as a module; this also intercepts adapters using
    # the same httpx module. TestClient itself uses the synchronous client.
    monkeypatch.setattr(httpx, "AsyncClient", mock_client)
    return Feed


def settings(client, **patch):
    response = client.put("/api/settings", json=patch)
    assert response.status_code == 200, response.text
    return response.json()


def import_one(client, symbol="IBM", price="912.75", day="2020-02-03"):
    text = f"Symbol,Price,Date,Volume\n{symbol},{price},{day},123\n"
    response = client.post("/api/stock-import", json={"csv": text})
    assert response.status_code == 200, response.text
    assert response.json()["imported"] == 1
    disclosure = " ".join(response.json()["warnings"]).lower()
    assert "stale" in disclosure and "not" in disclosure and "quote time" in disclosure
    return response.json()


def manual(items):
    return [item for item in items if item.get("cache_status") == "manual"]


def is_import(item):
    return item.get("cache_status") == "manual" and "import" in item.get("source", "").lower()


def read_tables(*names):
    with db.read_connection() as conn:
        return {name: [tuple(row) for row in conn.execute(f'SELECT * FROM "{name}" ORDER BY rowid')]
                for name in names}


def quote_observation_is_not_user_quote_time(item):
    assert item["quote_date"] == "2020-02-03"
    observed = db.parse_timestamp(item["observed_at"])
    assert observed is not None and observed.year > 2020
    assert item.get("quote_timestamp") is None
    assert item["stock_volume_shares"] == 123
    assert item["risk_status"] == "manual only"
    reason = item["reason"].lower()
    assert "not" in reason and "quote time" in reason and "stale" in reason


def test_defaults_validation_and_legacy_settings_are_backward_compatible(client, upstream):
    initial = client.get("/api/dashboard").json()["settings"]
    assert initial["stock_source_mode"] == "both"
    assert initial["stock_provider"] == "alpha_vantage"
    assert initial["market_mode"] == "crypto"
    # An older saved settings document must acquire new defaults without losing
    # its pre-existing values; no destructive migration is required.
    with db.transaction() as conn:
        conn.execute("UPDATE settings SET data=? WHERE id=1", (json.dumps({
            "market_mode": "stocks", "stock_symbols": ["IBM"], "risk_percent": 2.5,
        }),))
    legacy = client.get("/api/dashboard").json()["settings"]
    assert legacy["stock_source_mode"] == "both"
    assert legacy["stock_provider"] == "alpha_vantage"
    assert legacy["market_mode"] == "stocks" and legacy["risk_percent"] == 2.5
    for patch in ({"stock_source_mode": "invalid"}, {"stock_provider": "invalid"},
                  {"stock_source_mode": None}, {"stock_provider": None}):
        assert client.put("/api/settings", json=patch).status_code == 422
    assert upstream.calls == []


def test_csv_only_scan_returns_import_without_upstream_or_provider_key(client, upstream, monkeypatch):
    settings(client, market_mode="stocks", stock_source_mode="csv", stock_symbols=["IBM"])
    import_one(client)
    key_loads = []

    def forbidden_key(*args, **kwargs):
        key_loads.append(True)
        raise AssertionError("CSV-only scan must not load a provider credential")

    monkeypatch.setattr(trading.market_data, "load_key", forbidden_key)
    monkeypatch.setattr(trading.market_data, "load_finnhub_key", forbidden_key)
    response = client.post("/api/scan")
    assert response.status_code == 200, response.text
    result = response.json()
    assert len(result["candidates"]) == 1
    item = result["candidates"][0]
    assert item["symbol"] == "IBM" and item["price_usd"] == 912.75
    assert is_import(item)
    assert item["source"] == "User CSV import"
    quote_observation_is_not_user_quote_time(item)
    assert manual(client.get("/api/dashboard").json()["candidates"]) == [item]
    assert upstream.calls == [] and key_loads == []


def test_default_mode_uses_generic_csv_import_source_label(client, upstream):
    # Generic user-provided CSV must not be attributed to a specific provider.
    settings(client, market_mode="stocks")
    import_one(client)
    item = manual(client.get("/api/dashboard").json()["candidates"])[0]
    assert item["source"] == "User CSV import"
    assert upstream.calls == []


def test_both_mode_returns_independent_api_and_csv_even_for_same_symbol(client, upstream):
    settings(client, market_mode="stocks", stock_source_mode="both", stock_symbols=["IBM"])
    import_one(client)
    response = client.post("/api/scan")
    assert response.status_code == 200, response.text
    items = response.json()["candidates"]
    assert len(items) == 2
    csv_quote = manual(items)[0]
    api_quote = next(x for x in items if x["source"] == "Alpha Vantage")
    assert is_import(csv_quote)
    assert csv_quote["source"] == "User CSV import"
    assert (csv_quote["symbol"], csv_quote["price_usd"]) == ("IBM", 912.75)
    assert (api_quote["symbol"], api_quote["price_usd"]) == ("IBM", 181.25)
    quote_observation_is_not_user_quote_time(csv_quote)
    assert api_quote["quote_date"] == "2020-01-02"
    assert db.parse_timestamp(api_quote["observed_at"]) is not None
    assert api_quote["risk_status"] == "manual only"
    shown = client.get("/api/dashboard").json()["candidates"]
    assert {(x["source"], x["price_usd"]) for x in shown} == {
        ("Alpha Vantage", 181.25), ("User CSV import", 912.75)}
    assert shown == items
    assert [r.url.host for r in upstream.calls] == ["www.alphavantage.co"]


@pytest.mark.parametrize("failure", ["quota", "empty"])
def test_both_mode_keeps_csv_when_api_has_no_result_and_reports_failure(client, upstream, failure):
    settings(client, market_mode="stocks", stock_source_mode="both", stock_symbols=["IBM"])
    import_one(client)
    upstream.alpha = failure
    response = client.post("/api/scan")
    assert response.status_code == 200, response.text
    result = response.json()
    assert len(result["candidates"]) == 1
    item = result["candidates"][0]
    assert is_import(item) and item["price_usd"] == 912.75
    assert item["symbol"] == "IBM" and item["source"] == "User CSV import"
    quote_observation_is_not_user_quote_time(item)
    warnings = " ".join(result["warnings"]).lower()
    if failure == "quota":
        assert "quota" in warnings or "rate limit" in warnings
        assert "invalid or missing api key" not in warnings
    else:
        assert "empty" in warnings or "invalid quote" in warnings or "no quote" in warnings
        assert "quota" not in warnings and "rate limit" not in warnings
    assert manual(client.get("/api/dashboard").json()["candidates"]) == [item]
    assert len(upstream.calls) == 1


def test_api_only_hides_but_does_not_delete_csv_on_scan_or_switch(client, upstream):
    settings(client, market_mode="stocks", stock_source_mode="both", stock_symbols=["IBM"])
    import_one(client)
    before = read_tables("manual_stock_quotes")
    settings(client, stock_source_mode="api")
    assert not manual(client.get("/api/dashboard").json()["candidates"])
    response = client.post("/api/scan")
    assert response.status_code == 200, response.text
    assert [(x["source"], x["price_usd"]) for x in response.json()["candidates"]] == [("Alpha Vantage", 181.25)]
    assert not manual(client.get("/api/dashboard").json()["candidates"])
    assert read_tables("manual_stock_quotes") == before
    settings(client, stock_source_mode="csv")
    visible = client.get("/api/dashboard").json()["candidates"]
    assert len(visible) == 1 and is_import(visible[0])
    quote_observation_is_not_user_quote_time(visible[0])
    assert read_tables("manual_stock_quotes") == before
    assert len(upstream.calls) == 1


def test_finnhub_api_mode_uses_separate_mocked_provider_and_timestamp(client, upstream):
    secret = "fake-finnhub-integration-key"
    assert client.get("/api/market-data/finnhub").json()["has_api_key"] is False
    configured = client.put("/api/market-data/finnhub", json={"api_key": secret})
    assert configured.status_code == 200 and configured.json()["has_api_key"] is True
    assert secret not in configured.text + client.get("/api/market-data/finnhub").text
    assert client.get("/api/market-data").json()["has_api_key"] is False
    settings(client, market_mode="stocks", stock_source_mode="api", stock_provider="finnhub", stock_symbols=["IBM"])
    before = datetime.now(timezone.utc)
    response = client.post("/api/scan")
    after = datetime.now(timezone.utc)
    assert response.status_code == 200, response.text
    item = response.json()["candidates"][0]
    assert item["symbol"] == "IBM" and item["source"] == "Finnhub" and item["price_usd"] == 202.5
    assert db.parse_timestamp(item["quote_timestamp"]) == datetime.fromtimestamp(upstream.finnhub_timestamp, timezone.utc)
    assert before <= db.parse_timestamp(item["observed_at"]) <= after
    assert db.parse_timestamp(item["quote_timestamp"]) < db.parse_timestamp(item["observed_at"])
    assert item["risk_status"] == "manual only" and item["stock_volume_shares"] is None
    assert "old" in item["reason"].lower() or "delay" in item["reason"].lower()
    assert [(r.url.host, r.url.path) for r in upstream.calls] == [("finnhub.io", "/api/v1/quote")]
    assert secret not in response.text + client.get("/api/dashboard").text


def test_simulation_scan_creates_25_synthetic_candidates_and_filters_on_switch(client, upstream, monkeypatch):
    symbols = [chr(ord("A") + i) for i in range(25)]
    settings(client, market_mode="stocks", stock_source_mode="simulation", stock_symbols=symbols)
    key_loads = []

    def forbidden_key(*args, **kwargs):
        key_loads.append(True)
        raise AssertionError("Simulation must not load any provider key")

    monkeypatch.setattr(trading.market_data, "load_key", forbidden_key)
    monkeypatch.setattr(trading.market_data, "load_finnhub_key", forbidden_key)
    import_one(client, symbol="A")
    result = client.post("/api/scan")
    assert result.status_code == 200, result.text
    candidates = result.json()["candidates"]
    assert len(candidates) == 25 and {item["symbol"] for item in candidates} == set(symbols)
    for item in candidates:
        assert item["asset_type"] == "stock" and item["price_usd"] > 0
        label = " ".join(str(item.get(key) or "") for key in ("source", "reason", "cache_status")).lower()
        assert "simulat" in label or "synthetic" in label
        assert item["source"] not in ("Alpha Vantage", "Finnhub") and not is_import(item)
        assert db.parse_timestamp(item["observed_at"]) is not None
        assert item["quote_date"] is None and item.get("quote_timestamp") is None
    shown = client.get("/api/dashboard").json()["candidates"]
    assert len(shown) == 25 and not manual(shown)
    settings(client, stock_source_mode="csv")
    csv_items = client.get("/api/dashboard").json()["candidates"]
    assert len(csv_items) == 1 and is_import(csv_items[0])
    settings(client, stock_source_mode="api")
    assert client.get("/api/dashboard").json()["candidates"] == []
    settings(client, stock_source_mode="simulation")
    assert len(client.get("/api/dashboard").json()["candidates"]) == 25
    assert upstream.calls == [] and key_loads == []


@pytest.mark.parametrize("mode", ["api", "csv", "both"])
def test_simulation_cycle_rejected_outside_simulation_mode(client, upstream, mode):
    settings(client, market_mode="stocks", stock_source_mode=mode, auto_paper=True)
    before = read_tables("account", "positions", "trades", "equity_snapshots")
    response = client.post("/api/simulation/stocks/cycle")
    assert response.status_code in (403, 409, 422), response.text
    assert read_tables("account", "positions", "trades", "equity_snapshots") == before
    assert upstream.calls == []


def test_simulation_cycle_isolated_from_real_ledger_crypto_and_provider_secrets(client, upstream, tmp_path):
    alpha_secret, finn_secret = "fake-alpha-isolation-secret", "fake-finnhub-isolation-secret"
    assert client.put("/api/market-data", json={"api_key": alpha_secret}).status_code == 200
    assert client.put("/api/market-data/finnhub", json={"api_key": finn_secret}).status_code == 200
    # Seed both real paper asset types and explicit marks, to detect accidental
    # changes even if a mistaken path would otherwise operate on an empty book.
    for symbol, asset_type, price in (("IBM", "stock", 50), ("BTC", "crypto", 100)):
        response = client.post("/api/trades", json={"symbol": symbol, "asset_type": asset_type,
                               "side": "BUY", "quantity": 1, "price": price, "reason": "synthetic isolation test"})
        assert response.status_code == 200, response.text
    for position in client.get("/api/dashboard").json()["positions"]:
        assert client.patch(f'/api/positions/{position["id"]}/mark', json={"price": 77}).status_code == 200
    settings(client, market_mode="both", stock_source_mode="simulation", auto_paper=True,
             stock_symbols=["IBM", "AAPL"])
    initial_demo = client.get("/api/simulation/stocks")
    assert initial_demo.status_code == 200, initial_demo.text
    assert initial_demo.json()["enabled"] is True
    ledger = read_tables("account", "positions", "trades", "equity_snapshots")
    with sqlite3.connect(tmp_path / "providers.sqlite3") as conn:
        secrets = {name: conn.execute(f'SELECT * FROM "{name}"').fetchall()
                   for name in ("market_credentials", "finnhub_market_credentials")}
    with db.read_connection() as conn:
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND (name LIKE 'stock_sim_%' OR name LIKE 'simulation_%')")]
    assert names, "Simulation must persist in dedicated tables"
    demo_before = read_tables(*names)
    cycle = client.post("/api/simulation/stocks/cycle")
    assert cycle.status_code == 200, cycle.text
    assert cycle.json()["step"] == initial_demo.json()["step"] + 1
    assert all(item["asset_type"] == "stock" for item in cycle.json()["candidates"])
    assert client.get("/api/simulation/stocks").status_code == 200
    assert read_tables(*names) != demo_before, "The demo cycle must update isolated demo state"
    assert read_tables("account", "positions", "trades", "equity_snapshots") == ledger
    with sqlite3.connect(tmp_path / "providers.sqlite3") as conn:
        assert {name: conn.execute(f'SELECT * FROM "{name}"').fetchall() for name in secrets} == secrets
    assert alpha_secret not in cycle.text and finn_secret not in cycle.text
    assert client.get("/api/market-data").json()["has_api_key"] is True
    assert client.get("/api/market-data/finnhub").json()["has_api_key"] is True
    assert upstream.calls == []  # includes DexScreener: simulation must never execute crypto


def test_source_change_while_scan_is_in_flight_aborts_commit(client, upstream, monkeypatch):
    settings(client, market_mode="stocks", stock_source_mode="api", stock_symbols=["IBM"])
    import_one(client)
    before = read_tables("candidates", "stock_quote_cache", "manual_stock_quotes")
    entered, release = threading.Event(), threading.Event()
    original = trading._scan_stocks

    async def delayed(*args, **kwargs):
        entered.set()
        if not await asyncio.to_thread(release.wait, 5):
            raise AssertionError("Test failed to release the in-flight scan")
        return await original(*args, **kwargs)

    monkeypatch.setattr(trading, "_scan_stocks", delayed)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client.post, "/api/scan")
        try:
            assert entered.wait(5), "API scan did not reach the provider boundary"
            settings(client, stock_source_mode="csv")
        finally:
            release.set()
        response = future.result(timeout=10)
    assert response.status_code >= 400, response.text
    assert "changed" in response.text.lower() and "scan" in response.text.lower()
    assert read_tables("candidates", "stock_quote_cache", "manual_stock_quotes") == before
    visible = client.get("/api/dashboard").json()["candidates"]
    assert len(visible) == 1 and is_import(visible[0])
