"""Market data tests use mock transports; no real market data or API keys."""
import asyncio
import logging
import sqlite3

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app import db, trading
from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("APP_ENCRYPTION_KEY", raising=False)
    with TestClient(app) as test_client:
        yield test_client


def quote(symbol="IBM", price="178.1000", volume="1274000", change="-1.2500%", day="2026-11-06"):
    return {"Global Quote": {"01. symbol": symbol, "05. price": price, "06. volume": volume,
                             "07. latest trading day": day, "10. change percent": change}}


@pytest.fixture
def feeds(monkeypatch):
    class Feeds:
        calls = []
        stock = {}
        dex_failure = False
        dex_pair = {"chainId": "solana", "pairAddress": "pair123", "baseToken": {"address": "TokenAAA", "symbol": "FOO"},
                    "priceUsd": "2", "liquidity": {"usd": 100000}, "volume": {"h24": 33000}}

    def handler(request):
        Feeds.calls.append(request)
        if request.url.host == "www.alphavantage.co":
            assert request.url.path == "/query"
            assert request.url.params["function"] == "GLOBAL_QUOTE"
            symbol = request.url.params["symbol"]
            result = Feeds.stock.get(symbol, quote(symbol))
            if isinstance(result, Exception):
                raise result
            if isinstance(result, httpx.Response):
                return result
            return httpx.Response(200, json=result)
        assert request.url.host == "api.dexscreener.com"
        if Feeds.dex_failure:
            return httpx.Response(503, text="Dex failure")
        if "token-profiles" in request.url.path:
            return httpx.Response(200, json=[{"chainId": "solana", "tokenAddress": "TokenAAA"}])
        return httpx.Response(200, json={"pairs": [Feeds.dex_pair]})

    original = httpx.AsyncClient
    def create_client(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(**kwargs)
    monkeypatch.setattr(trading.httpx, "AsyncClient", create_client)
    return Feeds


def stock_calls(feeds):
    return [call for call in feeds.calls if call.url.host == "www.alphavantage.co"]


def test_legacy_schema_migrates_without_data_loss(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    db_path = tmp_path / "tradeledger.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE settings (id INTEGER PRIMARY KEY, data TEXT NOT NULL)")
        conn.execute("INSERT INTO settings VALUES (1, '{\"risk_percent\": 2, \"auto_paper\": true}')")
        conn.execute("""CREATE TABLE candidates (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL,
            chain TEXT NOT NULL, pair_address TEXT NOT NULL, price_usd TEXT, liquidity_usd TEXT,
            volume_24h TEXT, price_change_24h TEXT, risk_status TEXT NOT NULL, reason TEXT NOT NULL,
            url TEXT, observed_at TEXT NOT NULL)""")
        conn.execute("INSERT INTO candidates(symbol,chain,pair_address,price_usd,risk_status,reason,observed_at) VALUES ('FOO','solana','p','2','eligible','old','2020-01-01T00:00:00Z')")
    db.init_db()
    db.init_db()  # idempotent
    data = trading.dashboard()
    assert data["settings"]["risk_percent"] == 2
    assert data["settings"]["auto_paper"] is True
    assert data["settings"]["market_mode"] == "crypto"
    assert data["settings"]["stock_symbols"] == ["IBM"]
    assert data["candidates"][0]["asset_type"] == "crypto"
    assert data["candidates"][0]["source"] == "DexScreener"
    assert data["candidates"][0]["quote_date"] is None
    assert data["candidates"][0]["stock_volume_shares"] is None


def test_validation_and_normalization(client):
    assert client.get("/api/dashboard").json()["settings"]["market_mode"] == "crypto"
    assert client.put("/api/settings", json={"market_mode": "stocks", "stock_symbols": [" ibm ", "brk.b", "bf-b"]}).json()["stock_symbols"] == ["IBM", "BRK.B", "BF-B"]
    bad = [[], [""], [" "], ["$IBM"], ["IBM/USD"], ["NYSE:IBM"], ["12"], ["abc..d"], ["ß"],
           ["toolongticker"], ["IBM", " ibm "], ["A"] * 21, [None], "IBM"]
    for symbols in bad:
        assert client.put("/api/settings", json={"stock_symbols": symbols}).status_code == 422
    for mode in ("stock", "STOCKS", "all", None):
        assert client.put("/api/settings", json={"market_mode": mode}).status_code == 422


def test_demo_ibm_stock_quote_only_and_no_mark_or_auto_buy(client, feeds):
    assert client.get("/api/market-data").json() == {"stock_provider": "Alpha Vantage", "has_api_key": False, "using_demo": True}
    assert client.post("/api/trades", json={"symbol": "IBM", "asset_type": "stock", "side": "BUY", "quantity": 1, "price": 150, "reason": "manual test"}).status_code == 200
    assert client.put("/api/settings", json={"market_mode": "stocks", "auto_paper": True}).status_code == 200
    cycle = client.post("/api/cycle")
    assert cycle.status_code == 200
    result = cycle.json()
    assert result["market_mode"] == "stocks" and result["profiles_scanned"] == 0
    assert result["trades"] == [] and any("demo" in w.lower() and "IBM" in w for w in result["warnings"])
    item = result["candidates"][0]
    assert item["symbol"] == "IBM" and item["asset_type"] == "stock" and item["source"] == "Alpha Vantage"
    assert item["price_usd"] == 178.1 and item["quote_date"] == "2026-11-06"
    assert item["stock_volume_shares"] == 1274000 and item["stock_change_percent"] == -1.25
    assert all(item[k] is None for k in ("liquidity_usd", "volume_24h", "price_change_24h", "volume_spike_ratio", "pair_created_at", "chain", "pair_address"))
    assert item["risk_status"] == "manual only" and "manual paper trades only" in item["reason"]
    assert stock_calls(feeds)[0].url.params["apikey"] == "demo"
    data = client.get("/api/dashboard").json()
    assert data["candidates"][0] == item
    assert data["positions"][0]["mark_price"] is None
    assert len(data["trades"]) == 1
    assert "not real-time" in data["disclosures"]["market_data"]
    # Defence in depth: auto_buy rejects even a forged eligible stock object.
    with pytest.raises(trading.TradeError):
        trading.auto_buy({**item, "risk_status": "eligible", "liquidity_usd": 999999, "volume_24h": 999999})


def test_no_key_non_ibm_not_sent_and_partial(client, feeds):
    client.put("/api/settings", json={"market_mode": "stocks", "stock_symbols": ["AAPL", "IBM", "MSFT"]})
    response = client.post("/api/scan")
    assert response.status_code == 200
    assert [c["symbol"] for c in response.json()["candidates"]] == ["IBM"]
    assert [r.url.params["symbol"] for r in stock_calls(feeds)] == ["IBM"]
    assert all(any(symbol in warning for warning in response.json()["warnings"]) for symbol in ("AAPL", "MSFT"))
    client.put("/api/settings", json={"stock_symbols": ["AAPL"]})
    response = client.post("/api/scan")
    assert response.status_code == 502 and "AAPL" in response.text and "key" in response.text.lower()
    assert len(stock_calls(feeds)) == 1
    assert client.get("/api/dashboard").json()["candidates"][0]["symbol"] == "IBM"


def test_encrypted_key_redacted_lifecycle_and_safe_network(client, tmp_path, feeds, caplog):
    key = "fake-very-secret-key"
    caplog.set_level(logging.INFO, logger="httpx")
    result = client.put("/api/market-data", json={"api_key": key})
    assert result.json() == {"stock_provider": "Alpha Vantage", "has_api_key": True, "using_demo": False}
    assert key not in result.text + client.get("/api/market-data").text + client.get("/api/dashboard").text
    assert key.encode() not in (tmp_path / "providers.sqlite3").read_bytes()
    with sqlite3.connect(tmp_path / "providers.sqlite3") as conn:
        encrypted = conn.execute("SELECT encrypted_key FROM market_credentials WHERE id=1").fetchone()[0]
    assert Fernet((tmp_path / ".encryption-key").read_bytes()).decrypt(encrypted).decode() == key
    client.put("/api/settings", json={"market_mode": "stocks", "stock_symbols": ["IBM", "AAPL"]})
    scan = client.post("/api/scan")
    assert scan.status_code == 200 and len(scan.json()["candidates"]) == 2
    assert all(r.url.params["apikey"] == key for r in stock_calls(feeds))
    assert key not in scan.text + client.get("/api/dashboard").text + caplog.text
    assert client.put("/api/market-data", json={"clear_key": True}).json()["using_demo"] is True
    assert not client.get("/api/market-data").json()["has_api_key"]


def test_key_validation_conflicts_and_decryption_failure(client, monkeypatch, feeds):
    key = "do-not-leak-this-secret"
    for payload in ({}, {"api_key": ""}, {"api_key": "demo"}, {"api_key": key, "clear_key": True},
                    {"api_key": key, "clear_key": "true"}, {"api_key": key, "extra": key},
                    {"api_key": "bad\nkey"}, {"api_key": None}):
        response = client.put("/api/market-data", json=payload)
        assert response.status_code == 422 and key not in response.text
    assert client.put("/api/market-data", json={"api_key": key}).status_code == 200
    monkeypatch.setenv("APP_ENCRYPTION_KEY", Fernet.generate_key().decode())
    assert client.get("/api/market-data").status_code == 503
    client.put("/api/settings", json={"market_mode": "stocks"})
    scan = client.post("/api/scan")
    assert scan.status_code == 502 and "decrypt" in scan.text and key not in scan.text
    assert not stock_calls(feeds)  # never silently fall back to demo on decryption failure
    assert client.put("/api/market-data", json={"clear_key": True}).status_code == 200
    assert client.get("/api/market-data").json()["using_demo"]


def test_modes_partial_success_and_all_failures_preserve_previous(client, feeds):
    client.put("/api/market-data", json={"api_key": "private-key"})
    client.put("/api/settings", json={"market_mode": "both", "stock_symbols": ["IBM", "AAPL"]})
    feeds.stock["AAPL"] = {"Error Message": "invalid request private-key"}
    result = client.post("/api/scan").json()
    assert result["market_mode"] == "both" and result["profiles_scanned"] == 1
    assert {c["asset_type"] for c in result["candidates"]} == {"stock", "crypto"}
    assert "AAPL" in " ".join(result["warnings"]) and "private-key" not in str(result)
    feeds.dex_failure = True
    feeds.stock["AAPL"] = quote("AAPL")
    result = client.post("/api/cycle").json()
    assert {c["symbol"] for c in result["candidates"]} == {"IBM", "AAPL"}
    assert any("DexScreener" in warning for warning in result["warnings"])
    saved = client.get("/api/dashboard").json()["candidates"]
    feeds.stock["IBM"] = {"Information": "rate limited private-key"}
    response = client.post("/api/scan")
    assert response.status_code == 502 and "private-key" not in response.text
    assert client.get("/api/dashboard").json()["candidates"] == saved
    client.put("/api/settings", json={"market_mode": "crypto"})
    assert client.get("/api/dashboard").json()["candidates"] == []
    client.put("/api/settings", json={"market_mode": "stocks"})
    assert len(client.get("/api/dashboard").json()["candidates"]) == 2


@pytest.mark.parametrize("payload", [
    {}, {"Global Quote": {}}, {"Global Quote": {"01. symbol": "OTHER"}},
    quote(day="2026-99-99"), quote(price="NaN"), quote(price="0"),
    quote(volume="-1"), quote(volume="1.5"), {"Error Message": "invalid"},
])
def test_invalid_stock_response_fails_closed(client, feeds, payload):
    client.put("/api/settings", json={"market_mode": "stocks"})
    feeds.stock["IBM"] = payload
    result = client.post("/api/scan")
    assert result.status_code == 502
    assert "IBM" in result.text
    assert client.get("/api/dashboard").json()["candidates"] == []


@pytest.mark.parametrize("response", [{"Note": "limit"}, {"Information": "quota"}, httpx.Response(429, text="quota")])
def test_quota_backpressure_stops_remaining_requests(client, feeds, response):
    client.put("/api/market-data", json={"api_key": "own-key"})
    client.put("/api/settings", json={"market_mode": "stocks", "stock_symbols": ["IBM", "AAPL", "MSFT"]})
    feeds.stock["AAPL"] = response
    result = client.post("/api/scan")
    assert result.status_code == 200
    assert [c["symbol"] for c in result.json()["candidates"]] == ["IBM"]
    assert [r.url.params["symbol"] for r in stock_calls(feeds)] == ["IBM", "AAPL"]
    assert any("MSFT" in w and "skipped" in w for w in result.json()["warnings"])


def test_both_stock_failure_does_not_run_stale_crypto_cycle(client, feeds):
    client.put("/api/settings", json={"market_mode": "both", "auto_paper": True})
    assert len(client.post("/api/scan").json()["candidates"]) == 2
    feeds.dex_failure = True
    # Only a fresh stock quote remains; an earlier eligible crypto quote cannot be traded.
    result = client.post("/api/cycle").json()
    assert [c["symbol"] for c in result["candidates"]] == ["IBM"]
    assert result["trades"] == []
    assert client.get("/api/dashboard").json()["trades"] == []
    assert [c["symbol"] for c in client.get("/api/dashboard").json()["candidates"]] == ["IBM"]


def test_http_failures_and_partial_symbols(client, feeds):
    client.put("/api/market-data", json={"api_key": "own-key"})
    client.put("/api/settings", json={"market_mode": "stocks", "stock_symbols": ["IBM", "AAPL", "MSFT"]})
    feeds.stock["IBM"] = httpx.Response(500, text="secret response")
    feeds.stock["AAPL"] = httpx.ConnectError("secret URL apikey=own-key")
    result = client.post("/api/scan")
    assert result.status_code == 200
    assert [c["symbol"] for c in result.json()["candidates"]] == ["MSFT"]
    assert len(result.json()["warnings"]) == 2
    assert "own-key" not in result.text and "secret" not in result.text


def test_stale_candidate_cannot_be_bought_after_failure_or_new_scan(client, feeds):
    client.put("/api/settings", json={"auto_paper": True})
    candidate = client.post("/api/scan").json()["candidates"][0]
    feeds.dex_failure = True
    assert client.post("/api/scan").status_code == 502
    with pytest.raises(trading.TradeError, match="stale"):
        trading.auto_buy(candidate)
    feeds.dex_failure = False
    candidate = client.post("/api/scan").json()["candidates"][0]
    assert client.post("/api/scan").status_code == 200
    with pytest.raises(trading.TradeError, match="stale"):
        trading.auto_buy(candidate)
    assert client.get("/api/dashboard").json()["trades"] == []


def test_partial_both_can_auto_buy_only_fresh_crypto(client, feeds):
    client.put("/api/settings", json={"market_mode": "both", "auto_paper": True})
    feeds.stock["IBM"] = {"Note": "rate limited"}
    cycle = client.post("/api/cycle")
    assert cycle.status_code == 200
    assert [item["symbol"] for item in cycle.json()["candidates"]] == ["FOO"]
    assert [trade["symbol"] for trade in cycle.json()["trades"]] == ["FOO"]
    assert any("IBM" in warning for warning in cycle.json()["warnings"])


def test_no_redirect_to_untrusted_host(client, feeds):
    client.put("/api/market-data", json={"api_key": "own-private-key"})
    client.put("/api/settings", json={"market_mode": "stocks"})
    feeds.stock["IBM"] = httpx.Response(302, headers={"Location": "https://evil.example/collect"})
    result = client.post("/api/scan")
    assert result.status_code == 502
    assert "own-private-key" not in result.text
    assert [r.url.host for r in feeds.calls] == ["www.alphavantage.co"]


def test_settings_changed_during_scan_cannot_trade_old_candidate(client, feeds, monkeypatch):
    client.put("/api/settings", json={"auto_paper": True})
    candidate = client.post("/api/scan").json()["candidates"][0]
    original = trading._scan_crypto
    async def change_mode(settings):
        result = await original(settings)
        trading.update_settings({"market_mode": "both"})
        return result
    monkeypatch.setattr(trading, "_scan_crypto", change_mode)
    response = client.post("/api/scan")
    assert response.status_code == 502 and "changed during scan" in response.text
    assert client.get("/api/dashboard").json()["candidates"][0]["symbol"] == "FOO"
    with pytest.raises(trading.TradeError, match="stale"):
        trading.auto_buy(candidate)


def test_timeout_is_reported_and_other_symbols_can_succeed(client, feeds):
    client.put("/api/market-data", json={"api_key": "own-key"})
    client.put("/api/settings", json={"market_mode": "stocks", "stock_symbols": ["IBM", "AAPL"]})
    feeds.stock["IBM"] = httpx.ReadTimeout("secret query apikey=own-key")
    response = client.post("/api/scan")
    assert response.status_code == 200
    assert [c["symbol"] for c in response.json()["candidates"]] == ["AAPL"]
    assert any("IBM" in warning and "timed out" in warning for warning in response.json()["warnings"])
    assert "own-key" not in response.text


def test_market_credentials_are_separate_from_llm(client):
    client.put("/api/llm/openai", json={"api_key": "llm-private-key"})
    assert client.get("/api/market-data").json()["using_demo"]
    client.put("/api/market-data", json={"api_key": "stock-private-key"})
    assert client.get("/api/market-data").json()["has_api_key"]
    client.put("/api/market-data", json={"clear_key": True})
    providers = client.get("/api/llm").json()["providers"]
    assert next(p for p in providers if p["id"] == "openai")["has_key"]
