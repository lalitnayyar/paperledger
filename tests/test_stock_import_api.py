"""Manual import integration tests: isolated database and mock-only market feeds."""
import asyncio
import logging
import socket
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db, trading, yahoo_quotes
from app.main import app, STOCK_IMPORT_MAX_REQUEST_BYTES, StockImportBodyLimitMiddleware


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("APP_ENCRYPTION_KEY", raising=False)
    # An accidental real DNS/network request must not escape the test suite.
    def no_dns(*args, **kwargs):
        raise AssertionError("Real network access is forbidden in import tests")
    monkeypatch.setattr(socket, "getaddrinfo", no_dns)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def feeds(monkeypatch):
    calls = []
    original_client = httpx.AsyncClient

    def handler(request):
        calls.append(request)
        if request.url.host == "www.alphavantage.co":
            assert request.url.path == "/query"
            symbol = request.url.params["symbol"]
            return httpx.Response(200, json={"Global Quote": {
                "01. symbol": symbol, "05. price": "87.50", "06. volume": "1000",
                "07. latest trading day": "2026-11-06", "10. change percent": "2%"}})
        assert request.url.host == "api.dexscreener.com", "Unexpected market host"
        if "token-profiles" in request.url.path:
            return httpx.Response(200, json=[{"chainId": "solana", "tokenAddress": "TokenAAA"}])
        return httpx.Response(200, json={"pairs": [{
            "chainId": "solana", "pairAddress": "pair123",
            "baseToken": {"address": "TokenAAA", "symbol": "FOO"},
            "priceUsd": "2", "liquidity": {"usd": 100000}, "volume": {"h24": 33000}}]})

    def mock_client(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_client(**kwargs)
    monkeypatch.setattr(trading.httpx, "AsyncClient", mock_client)
    return calls


def imported(client):
    return [c for c in client.get("/api/dashboard").json()["candidates"] if c["cache_status"] == "manual"]


def unique_symbols(n):
    return [chr(ord("A") + i) for i in range(n)]


def test_import_partial_invalid_optional_fields_and_log_redaction(client, feeds, caplog):
    caplog.set_level(logging.INFO)
    assert client.put("/api/settings", json={"market_mode": "stocks"}).status_code == 200
    secret = "csv-private-never-log-or-return"
    csv = ("Symbol,Current Price,Date,Volume,Notes\n"
           f"AAPL,123.45,,,{secret}\n"
           "bad/symbol,10,,,x\n"
           "MSFT,22,11/06/2026,0,x\n"
           "aapl,17,,,x\n")
    response = client.post("/api/stock-import", json={"csv": csv})
    assert response.status_code == 200
    result = response.json()
    assert result["imported"] == 2
    assert any("Row 3" in warning and "ticker" in warning for warning in result["warnings"])
    assert any("Row 5" in warning and "duplicate" in warning for warning in result["warnings"])
    assert any("not verified" in w and "stale" in w for w in result["warnings"])
    assert any("optional" in w and "not quote time" in w for w in result["warnings"])
    items = imported(client)
    assert [c["symbol"] for c in items] == ["AAPL", "MSFT"]  # neither is in the IBM watchlist
    assert set(items[0]) == set(trading._CANDIDATE_FIELDS)
    assert items[0]["price_usd"] == 123.45 and isinstance(items[0]["price_usd"], (float, int))
    assert items[0]["quote_date"] is None and items[0]["stock_volume_shares"] is None
    assert items[1]["quote_date"] == "2026-11-06" and items[1]["stock_volume_shares"] == 0
    for item in items:
        assert item["asset_type"] == "stock" and item["source"] == "User import (Yahoo Finance)"
        assert item["risk_status"] == "manual only" and item["cache_status"] == "manual"
        assert all(item[k] is None for k in ("chain", "pair_address", "liquidity_usd", "volume_24h", "price_change_24h", "volume_spike_ratio", "pair_created_at", "stock_change_percent", "url"))
        assert db.parse_timestamp(item["observed_at"]) is not None
        assert all(word in item["reason"].lower() for word in ("user-provided", "not independently verified", "stale", "optional", "not the quote time", "manual"))
    assert items[0]["observed_at"] == items[1]["observed_at"]
    dashboard = client.get("/api/dashboard").json()
    assert "not the quote time" in dashboard["disclosures"]["manual_import"]
    messages = [a["message"] for a in dashboard["activity"] if a["message"].startswith("Manual stock import")]
    assert messages == ["Manual stock import replaced: 2 quote(s)."]
    assert secret not in response.text + str(dashboard) + caplog.text
    assert "bad/symbol" not in response.text + str(dashboard) + caplog.text
    assert feeds == []  # import and dashboard do not contact any provider


def test_invalid_empty_and_over_25_never_replace_previous_import(client, feeds, caplog):
    client.put("/api/settings", json={"market_mode": "stocks"})
    assert client.post("/api/stock-import", json={"csv": "Symbol,Price\nAAPL,12\n"}).status_code == 200
    previous = imported(client)
    rows26 = "Symbol,Price\n" + "".join(f"{s},1\n" for s in unique_symbols(26))
    bad_csv = ["", "Symbol,Price\n", "Symbol,Price\nAAPL,-1\n",
               "wrong,header\nsecret,100\n", rows26, 'Symbol,Price\nIBM,"bad']
    for text in bad_csv:
        response = client.post("/api/stock-import", json={"csv": text})
        assert response.status_code == 422
        assert "No quotes imported" in response.json()["detail"]
        assert "secret" not in response.text
        assert imported(client) == previous
    assert "more than 25" in client.post("/api/stock-import", json={"csv": rows26}).json()["detail"]
    assert len([a for a in client.get("/api/dashboard").json()["activity"] if a["message"].startswith("Manual stock import")]) == 1
    assert feeds == []


def test_replace_atomically_and_preserve_alpha_candidates_and_cache(client, feeds):
    client.put("/api/settings", json={"market_mode": "stocks"})
    scanned = client.post("/api/scan").json()["candidates"]
    assert len(scanned) == 1 and scanned[0]["source"] == "Alpha Vantage"
    assert client.post("/api/stock-import", json={"csv": "Symbol,Price\nIBM,999\nAAPL,20\n"}).json()["imported"] == 2
    items = client.get("/api/dashboard").json()["candidates"]
    assert [(c["symbol"], c["source"], c["price_usd"]) for c in items] == [
        ("IBM", "Alpha Vantage", 87.5), ("IBM", "User import (Yahoo Finance)", 999),
        ("AAPL", "User import (Yahoo Finance)", 20)]
    with db.read_connection() as conn:
        before = [tuple(r) for r in conn.execute("SELECT * FROM stock_quote_cache")]
    assert client.post("/api/stock-import", json={"csv": "Symbol,Price\nMSFT,33\n"}).json()["imported"] == 1
    assert [x["symbol"] for x in imported(client)] == ["MSFT"]
    all_candidates = client.get("/api/dashboard").json()["candidates"]
    assert len(all_candidates) == 2 and all_candidates[0] == scanned[0]
    with db.read_connection() as conn:
        assert [tuple(r) for r in conn.execute("SELECT * FROM stock_quote_cache")] == before
        assert conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 1
    # Changing the Alpha watchlist does not delete imports.
    client.put("/api/settings", json={"stock_symbols": ["AAPL"]})
    assert [x["symbol"] for x in imported(client)] == ["MSFT"]
    assert all(r.url.host == "www.alphavantage.co" for r in feeds)


def test_database_error_rolls_back_complete_replacement(client):
    client.put("/api/settings", json={"market_mode": "stocks"})
    client.post("/api/stock-import", json={"csv": "Symbol,Price\nIBM,12\n"})
    before = imported(client)
    with db.transaction() as conn:
        conn.execute("""CREATE TRIGGER fail_second_manual BEFORE INSERT ON manual_stock_quotes
            WHEN NEW.symbol='MSFT' BEGIN SELECT RAISE(ABORT,'test rollback'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        client.post("/api/stock-import", json={"csv": "Symbol,Price\nAAPL,20\nMSFT,30\n"})
    assert imported(client) == before


def test_modes_scan_cycle_and_restart_do_not_trade_or_mark_imports(client, feeds):
    client.put("/api/settings", json={"market_mode": "stocks", "auto_paper": True})
    assert client.post("/api/trades", json={"symbol": "AAPL", "asset_type": "stock", "side": "BUY", "quantity": 1, "price": 10, "reason": "paper"}).status_code == 200
    assert client.post("/api/stock-import", json={"csv": "Symbol,Price\nAAPL,99\nXYZ,5\n"}).status_code == 200
    original = imported(client)
    cycle = client.post("/api/cycle")
    assert cycle.status_code == 200 and cycle.json()["trades"] == []
    assert [c["source"] for c in cycle.json()["candidates"]] == ["Alpha Vantage"]
    data = client.get("/api/dashboard").json()
    assert len(data["trades"]) == 1 and data["positions"][0]["mark_price"] is None
    assert data["positions"][0]["mark_source"] is None and data["positions"][0]["mark_timestamp"] is None
    assert imported(client) == original
    for item in original:
        with pytest.raises(trading.TradeError):
            trading.auto_buy({**item, "risk_status": "eligible"})
    # A crypto-only scan cannot erase imports or mark the stock position.
    client.put("/api/settings", json={"market_mode": "crypto"})
    assert imported(client) == []
    assert client.post("/api/scan").status_code == 200
    assert client.post("/api/cycle").status_code == 200
    assert client.get("/api/dashboard").json()["positions"][0]["mark_price"] is None
    # Exercise the same SQLite initialization sequence a new process uses.
    db._initialized_paths.discard(str(db.data_path().absolute()))
    with TestClient(app) as restarted:
        assert imported(restarted) == []
        restarted.put("/api/settings", json={"market_mode": "both"})
        assert imported(restarted) == original
        assert restarted.get("/api/dashboard").json()["positions"][0]["mark_price"] is None
        restarted.put("/api/settings", json={"market_mode": "stocks"})
        assert imported(restarted) == original
        assert len(restarted.get("/api/dashboard").json()["trades"]) == 1
    assert {r.url.host for r in feeds} == {"www.alphavantage.co", "api.dexscreener.com"}


def test_failed_stock_scan_preserves_import_but_does_not_consider_it_a_feed(client, monkeypatch):
    client.put("/api/settings", json={"market_mode": "stocks"})
    client.post("/api/stock-import", json={"csv": "Symbol,Price\nAAPL,99\n"})
    previous = imported(client)
    async def failed_feed(symbols):
        return [], ["Mock provider unavailable."]
    monkeypatch.setattr(trading, "_scan_stocks", failed_feed)
    response = client.post("/api/scan")
    assert response.status_code == 502
    assert imported(client) == previous


def test_25_unique_watchlist_and_import_accepted_26_rejected(client):
    symbols = unique_symbols(26)
    ok = client.put("/api/settings", json={"market_mode": "stocks", "stock_symbols": symbols[:25]})
    assert ok.status_code == 200 and ok.json()["stock_symbols"] == symbols[:25]
    assert client.put("/api/settings", json={"stock_symbols": symbols}).status_code == 422
    assert client.get("/api/dashboard").json()["settings"]["stock_symbols"] == symbols[:25]
    csv = "Symbol,Price\n" + "".join(f"{s},{i+1}\n" for i, s in enumerate(symbols[:25]))
    assert client.post("/api/stock-import", json={"csv": csv}).json()["imported"] == 25
    assert [c["symbol"] for c in imported(client)] == symbols[:25]
    assert client.post("/api/stock-import", json={"csv": csv + "Z,26\n"}).status_code == 422
    assert [c["symbol"] for c in imported(client)] == symbols[:25]


def test_pydantic_is_strict_redacted_and_bounds_utf8_bytes(client, caplog):
    assert client.put("/api/settings", json={"market_mode": "stocks"}).status_code == 200
    client.post("/api/stock-import", json={"csv": "Symbol,Price\nIBM,3\n"})
    before = imported(client)
    secret = "do-not-echo-extra-name-or-data"
    invalid = [{}, {"csv": 42}, {"csv": ["Symbol,Price"]}, {"csv": None},
               {"csv": "Symbol,Price\nIBM,3", secret: secret},
               {"csv": "x" * (yahoo_quotes.MAX_IMPORT_BYTES + 1)},
               # Fewer than 128 Ki characters but more than 128 Ki UTF-8 bytes.
               {"csv": "é" * (yahoo_quotes.MAX_IMPORT_BYTES // 2 + 1)}]
    for value in invalid:
        response = client.post("/api/stock-import", json=value)
        assert response.status_code == 422 and secret not in response.text + caplog.text
        assert imported(client) == before
    # A JSON escape can represent a lone surrogate; reject it without encoding errors or echo.
    response = client.post("/api/stock-import", content=b'{"csv":"\\ud800"}', headers={"content-type": "application/json"})
    assert response.status_code == 422 and imported(client) == before
    # Exact parser byte limit remains admissible (empty CSV records are ignored).
    base = "Symbol,Price\nAAPL,12\n"
    exact = base + "\n" * (yahoo_quotes.MAX_IMPORT_BYTES - len(base.encode("utf-8")))
    assert len(exact.encode("utf-8")) == yahoo_quotes.MAX_IMPORT_BYTES
    response = client.post("/api/stock-import", json={"csv": exact})
    assert response.status_code == 200 and response.json()["imported"] == 1
    assert [c["symbol"] for c in imported(client)] == ["AAPL"]


def test_maximum_parser_values_fit_sqlite_and_null_values_round_trip(client):
    client.put("/api/settings", json={"market_mode": "stocks"})
    data = "Symbol,Price,Volume,Date\nIBM,1e18,1e18,\nAAPL,0.000000000000000001,,\n"
    assert client.post("/api/stock-import", json={"csv": data}).json()["imported"] == 2
    items = imported(client)
    assert items[0]["price_usd"] == 1e18 and items[0]["stock_volume_shares"] == 10**18
    assert items[0]["quote_date"] is None
    assert items[1]["price_usd"] == 1e-18 and items[1]["stock_volume_shares"] is None


def test_request_body_stream_is_bounded_without_content_length():
    async def never_called(scope, receive, send):
        raise AssertionError("Oversized stream reached the JSON parser")
    sent = []
    chunks = iter([b"x" * 65536] * (STOCK_IMPORT_MAX_REQUEST_BYTES // 65536 + 1))
    async def receive():
        return {"type": "http.request", "body": next(chunks), "more_body": True}
    async def send(message):
        sent.append(message)
    scope = {"type": "http", "method": "POST", "path": "/api/stock-import", "headers": []}
    asyncio.run(StockImportBodyLimitMiddleware(never_called)(scope, receive, send))
    assert sent[0]["type"] == "http.response.start" and sent[0]["status"] == 422
    assert b"size limit" in sent[1]["body"] and b"xxxx" not in sent[1]["body"]
