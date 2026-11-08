"""Market data tests use mock transports; no real market data or API keys."""
import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

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
    assert client.get("/api/dashboard").json()["candidates"] == []


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
    assert response.status_code == 200 and "private-key" not in response.text
    assert all(c["cache_status"] == "cached" for c in response.json()["candidates"])
    assert any("only cached" in warning.lower() for warning in response.json()["warnings"])
    assert [c["observed_at"] for c in response.json()["candidates"]] == [c["observed_at"] for c in saved]
    assert client.get("/api/dashboard").json()["candidates"] == response.json()["candidates"]
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


def age_stock_cache(hours=7, symbols=None):
    """Expire cached quotes and matching dashboard rows without changing prices."""
    observed = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat().replace('+00:00', 'Z')
    with db.transaction() as conn:
        if symbols is None:
            conn.execute("UPDATE stock_quote_cache SET observed_at=?", (observed,))
            conn.execute("UPDATE candidates SET observed_at=? WHERE asset_type='stock'", (observed,))
        else:
            for symbol in symbols:
                conn.execute("UPDATE stock_quote_cache SET observed_at=? WHERE symbol=?", (observed, symbol))
                conn.execute("UPDATE candidates SET observed_at=? WHERE asset_type='stock' AND symbol=?", (observed, symbol))
    return observed


def test_stock_cache_fresh_then_cached_across_restart_without_requests(client, feeds):
    client.put('/api/market-data', json={'api_key': 'mock-cache-key'})
    client.put('/api/settings', json={'market_mode': 'stocks', 'stock_symbols': ['AAPL', 'NCNO']})
    first = client.post('/api/scan').json()
    assert [x['cache_status'] for x in first['candidates']] == ['fresh', 'fresh']
    assert all(x['quote_date'] == '2026-11-06' for x in first['candidates'])
    assert len(stock_calls(feeds)) == 2
    second = client.post('/api/scan').json()
    assert [x['cache_status'] for x in second['candidates']] == ['cached', 'cached']
    assert [x['observed_at'] for x in second['candidates']] == [x['observed_at'] for x in first['candidates']]
    assert any('only cached' in w.lower() for w in second['warnings'])
    assert len(stock_calls(feeds)) == 2
    # Force the same initialization path as a new process without changing the DB.
    db._initialized_paths.discard(str(db.data_path().absolute()))
    with TestClient(app) as restarted:
        after = restarted.post('/api/scan')
        assert after.status_code == 200
        assert [x['cache_status'] for x in after.json()['candidates']] == ['cached', 'cached']
        assert [x['observed_at'] for x in after.json()['candidates']] == [x['observed_at'] for x in first['candidates']]
        assert restarted.get('/api/dashboard').json()['candidates'] == after.json()['candidates']
    assert len(stock_calls(feeds)) == 2


def test_partial_refresh_rate_limit_retains_expired_quotes_and_new_quote(client, feeds):
    symbols = ['AAPL', 'NCNO', 'AMZN', 'BMY', 'S', 'SMCI', 'POET', 'MARA']
    client.put('/api/market-data', json={'api_key': 'mock-partial-key'})
    client.put('/api/settings', json={'market_mode': 'stocks', 'stock_symbols': symbols})
    assert len(client.post('/api/scan').json()['candidates']) == 8
    old_observed = age_stock_cache()
    old = {x['symbol']: x for x in client.get('/api/dashboard').json()['candidates']}
    assert all(x['cache_status'] == 'stale' for x in old.values())
    feeds.calls.clear()
    feeds.stock['AAPL'] = quote('AAPL', price='191.50', day='2026-11-05')
    feeds.stock['NCNO'] = {'Information': 'Our standard API rate limit is 25 requests per day. secret mock-partial-key'}
    response = client.post('/api/scan')
    assert response.status_code == 200
    result = response.json()
    stocks = {x['symbol']: x for x in result['candidates']}
    assert list(stocks) == symbols
    assert [r.url.params['symbol'] for r in stock_calls(feeds)] == ['AAPL', 'NCNO']
    assert stocks['AAPL']['cache_status'] == 'fresh' and stocks['AAPL']['price_usd'] == 191.5
    assert stocks['AAPL']['quote_date'] == '2026-11-05' and stocks['AAPL']['observed_at'] != old_observed
    for symbol in symbols[1:]:
        assert stocks[symbol]['cache_status'] == 'stale'
        assert stocks[symbol]['price_usd'] == old[symbol]['price_usd']
        assert stocks[symbol]['observed_at'] == old_observed
        assert stocks[symbol]['quote_date'] == old[symbol]['quote_date']
    for symbol in symbols[2:]:
        assert any(symbol in w and 'skipped' in w.lower() for w in result['warnings'])
    assert any('quota' in w.lower() for w in result['warnings'])
    assert 'mock-partial-key' not in response.text
    assert client.get('/api/dashboard').json()['candidates'] == result['candidates']
    # A later scan uses the successfully saved AAPL quote instead of fetching it again.
    feeds.calls.clear()
    again = client.post('/api/scan').json()
    assert [r.url.params['symbol'] for r in stock_calls(feeds)] == ['NCNO']
    assert again['candidates'][0]['cache_status'] == 'cached'
    assert again['candidates'][0]['observed_at'] == stocks['AAPL']['observed_at']


def test_refresh_expired_quote_replaces_cache_and_retains_current_fetch_time(client, feeds):
    client.put('/api/market-data', json={'api_key': 'mock-key'})
    client.put('/api/settings', json={'market_mode': 'stocks', 'stock_symbols': ['AAPL']})
    client.post('/api/scan')
    old = age_stock_cache()
    feeds.stock['AAPL'] = quote('AAPL', price='203.1234', day='2026-10-30')
    refreshed = client.post('/api/scan').json()['candidates'][0]
    assert refreshed['cache_status'] == 'fresh' and refreshed['price_usd'] == 203.1234
    assert refreshed['quote_date'] == '2026-10-30' and refreshed['observed_at'] != old
    cached = client.post('/api/scan').json()['candidates'][0]
    assert cached['cache_status'] == 'cached'
    assert cached['observed_at'] == refreshed['observed_at'] and cached['quote_date'] == refreshed['quote_date']
    assert len(stock_calls(feeds)) == 2


def test_changed_watchlist_removes_cache_and_fetches_only_missing(client, feeds):
    client.put('/api/market-data', json={'api_key': 'mock-key'})
    client.put('/api/settings', json={'market_mode': 'stocks', 'stock_symbols': ['AAPL', 'NCNO']})
    client.post('/api/scan')
    client.put('/api/settings', json={'stock_symbols': ['NCNO', 'AMZN']})
    assert [c['symbol'] for c in client.get('/api/dashboard').json()['candidates']] == ['NCNO']
    feeds.calls.clear()
    second = client.post('/api/scan').json()['candidates']
    assert [(c['symbol'], c['cache_status']) for c in second] == [('NCNO', 'cached'), ('AMZN', 'fresh')]
    assert [r.url.params['symbol'] for r in stock_calls(feeds)] == ['AMZN']
    client.put('/api/settings', json={'stock_symbols': ['NCNO', 'AAPL']})
    assert [c['symbol'] for c in client.get('/api/dashboard').json()['candidates']] == ['NCNO']
    feeds.calls.clear()
    third = client.post('/api/scan').json()['candidates']
    assert [(c['symbol'], c['cache_status']) for c in third] == [('NCNO', 'cached'), ('AAPL', 'fresh')]
    assert [r.url.params['symbol'] for r in stock_calls(feeds)] == ['AAPL']
    with db.read_connection() as conn:
        assert {r['symbol'] for r in conn.execute('SELECT symbol FROM stock_quote_cache')} == {'NCNO', 'AAPL'}


@pytest.mark.parametrize(('payload', 'expected'), [
    ({'Note': 'Our standard API call frequency is 5 calls per minute.'}, 'quota'),
    ({'Information': 'You have exceeded your request quota.'}, 'quota'),
    ({'Error Message': 'The API key is invalid.'}, 'invalid or missing API key'),
    ({'Information': 'Invalid API key, please obtain one.'}, 'invalid or missing API key'),
    ({'Information': 'This premium endpoint requires a premium subscription.'}, 'premium entitlement'),
    ({'Error Message': 'Not available on your current subscription.'}, 'premium entitlement'),
    ({'Information': 'Provider notice without enough details.'}, 'cause is unknown'),
    ({'Note': 123}, 'cause is unknown'),
    (httpx.Response(429, text='private provider text'), 'quota'),
    (httpx.Response(400, json={'Information': 'You have exceeded your request quota.'}), 'quota'),
    (httpx.Response(503, json={'Information': 'This premium endpoint requires a subscription.'}), 'premium entitlement'),
    (httpx.Response(401, text='private provider text'), 'HTTP 401'),
    (httpx.Response(403, json={'Information': 'API key is invalid'}), 'invalid or missing API key'),
])
def test_provider_notice_classification_stops_requests_and_redacts(client, feeds, caplog, payload, expected):
    secret = 'fake-sensitive-key-8937'
    caplog.set_level(logging.INFO, logger='httpx')
    client.put('/api/market-data', json={'api_key': secret})
    client.put('/api/settings', json={'market_mode': 'stocks', 'stock_symbols': ['AAPL', 'NCNO', 'AMZN']})
    feeds.stock['NCNO'] = payload
    response = client.post('/api/scan')
    assert response.status_code == 200
    assert [r.url.params['symbol'] for r in stock_calls(feeds)] == ['AAPL', 'NCNO']
    assert expected.lower() in response.text.lower()
    assert any('AMZN' in w and 'skipped' in w for w in response.json()['warnings'])
    if expected not in ('invalid or missing API key',):
        assert 'invalid or missing API key' not in response.text
    assert secret not in response.text + client.get('/api/dashboard').text + caplog.text
    assert 'private provider text' not in response.text + caplog.text
    assert 'www.alphavantage.co/query' not in caplog.text


def test_unknown_notice_with_echoed_secret_is_not_a_quota_or_invalid_key(client, feeds, caplog):
    secret = 'mock-unique-secret-2134'
    client.put('/api/market-data', json={'api_key': secret})
    client.put('/api/settings', json={'market_mode': 'stocks', 'stock_symbols': ['AAPL', 'NCNO']})
    feeds.stock['AAPL'] = {'Information': f'Unexpected {secret} https://example.test/?apikey={secret}'}
    response = client.post('/api/scan')
    assert response.status_code == 502
    assert len(stock_calls(feeds)) == 1 and 'NCNO' in response.text and 'skipped' in response.text
    assert 'cause is unknown' in response.text and 'quota' not in response.text.lower()
    assert secret not in response.text + client.get('/api/dashboard').text + caplog.text
    assert 'example.test' not in response.text + caplog.text


def test_symbol_errors_continue_then_valid_quote_is_persisted(client, feeds):
    client.put('/api/market-data', json={'api_key': 'mock-key'})
    client.put('/api/settings', json={'market_mode': 'stocks', 'stock_symbols': ['AAPL', 'NCNO', 'AMZN']})
    feeds.stock['AAPL'] = {'Error Message': 'Invalid API call; please check the symbol.'}
    feeds.stock['NCNO'] = {'Global Quote': {}}
    result = client.post('/api/scan').json()
    assert [r.url.params['symbol'] for r in stock_calls(feeds)] == ['AAPL', 'NCNO', 'AMZN']
    assert [c['symbol'] for c in result['candidates']] == ['AMZN']
    assert len(result['warnings']) == 2
    feeds.calls.clear()
    client.post('/api/scan')
    assert [r.url.params['symbol'] for r in stock_calls(feeds)] == ['AAPL', 'NCNO']


def test_expired_cache_fallback_when_key_becomes_unavailable(client, feeds, monkeypatch):
    client.put('/api/market-data', json={'api_key': 'mock-secret'})
    client.put('/api/settings', json={'market_mode': 'stocks', 'stock_symbols': ['AAPL']})
    first = client.post('/api/scan').json()['candidates'][0]
    old = age_stock_cache()
    monkeypatch.setenv('APP_ENCRYPTION_KEY', Fernet.generate_key().decode())
    feeds.calls.clear()
    result = client.post('/api/scan')
    assert result.status_code == 200 and not stock_calls(feeds)
    item = result.json()['candidates'][0]
    assert item['cache_status'] == 'stale' and item['observed_at'] == old and item['price_usd'] == first['price_usd']
    assert any('decrypt' in w for w in result.json()['warnings'])
    assert any('only cached' in w.lower() for w in result.json()['warnings'])
    assert 'mock-secret' not in result.text


def test_cached_stock_with_failed_crypto_cycle_never_trades_old_crypto(client, feeds):
    client.put('/api/settings', json={'market_mode': 'both', 'auto_paper': True})
    original = client.post('/api/scan').json()['candidates']
    assert next(c for c in original if c['asset_type'] == 'crypto')['cache_status'] is None
    feeds.dex_failure = True
    result = client.post('/api/cycle').json()
    assert [(c['symbol'], c['cache_status']) for c in result['candidates']] == [('IBM', 'cached')]
    assert result['trades'] == [] and any('only cached' in w.lower() for w in result['warnings'])
    with pytest.raises(trading.TradeError, match='stale'):
        trading.auto_buy(next(c for c in original if c['asset_type'] == 'crypto'))
    assert client.get('/api/dashboard').json()['trades'] == []


@pytest.mark.parametrize(('timestamp', 'expected'), [
    ('2026-11-08T11:00:00Z', True),
    ('2026-11-08T14:00:00+03:00', True),
    ('2026-11-08T06:00:00Z', False),  # exactly six hours expires
    ('2026-11-08T12:00:01Z', False),  # future time is never fresh
    ('2026-11-08T11:00:00', False),  # timezone must not be guessed
    ('bad timestamp', False),
    (None, False),
])
def test_cache_timestamp_parsing_and_ttl_boundary(timestamp, expected):
    now = datetime(2026, 11, 8, 12, tzinfo=timezone.utc)
    assert trading._stock_cache_is_fresh(timestamp, now) is expected


def test_migration_seeds_valid_watched_legacy_quotes_without_new_times(tmp_path, monkeypatch, feeds):
    import json
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    db.init_db()
    recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace('+00:00', 'Z')
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat().replace('+00:00', 'Z')
    with sqlite3.connect(db.data_path()) as conn:
        conn.execute('DROP TABLE stock_quote_cache')
        conn.execute('ALTER TABLE candidates DROP COLUMN cache_status')
        conn.execute('UPDATE settings SET data=? WHERE id=1', (json.dumps({'market_mode': 'stocks', 'stock_symbols': ['AAPL', 'NCNO', 'AMZN']}),))
        sql = '''INSERT INTO candidates(symbol,chain,pair_address,price_usd,risk_status,reason,observed_at,
                 asset_type,source,quote_date,stock_volume_shares,stock_change_percent) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)'''
        conn.execute(sql, ('AAPL', '', '', '101.1234', 'manual only', 'legacy', recent, 'stock', 'Alpha Vantage', '2026-11-06', 1000, '1.25'))
        conn.execute(sql, ('NCNO', '', '', '22', 'manual only', 'legacy', old, 'stock', 'Alpha Vantage', '2026-11-05', 200, None))
        conn.execute(sql, ('AMZN', '', '', '45', 'manual only', 'legacy', 'missing zone', 'stock', 'Alpha Vantage', '2026-11-06', 200, None))
        conn.execute(sql, ('MSFT', '', '', '30', 'manual only', 'legacy', recent, 'stock', 'Alpha Vantage', '2026-11-06', 200, None))
        conn.execute(sql, ('FOO', 'solana', 'pair', '1', 'eligible', 'legacy', old, 'crypto', 'DexScreener', None, None, None))
    db.init_db()
    db.init_db()
    with db.read_connection() as conn:
        cache = {r['symbol']: dict(r) for r in conn.execute('SELECT * FROM stock_quote_cache')}
        assert set(cache) == {'AAPL', 'NCNO'}
        assert cache['AAPL']['observed_at'] == recent and cache['NCNO']['observed_at'] == old
        assert conn.execute('SELECT COUNT(*) FROM candidates').fetchone()[0] == 5
        assert conn.execute('SELECT cash FROM account').fetchone()[0] == '100000.0'
    # Only the recent quote is a cache hit; expired NCNO falls back when provider stops.
    feeds.stock['NCNO'] = {'Note': 'rate limit'}
    with TestClient(app) as migrated:
        assert migrated.put('/api/market-data', json={'api_key': 'mock-migration-key'}).status_code == 200
        response = migrated.post('/api/scan')
        assert response.status_code == 200
        items = response.json()['candidates']
        assert [(x['symbol'], x['cache_status']) for x in items] == [('AAPL', 'cached'), ('NCNO', 'stale')]
        assert items[0]['observed_at'] == recent and items[1]['observed_at'] == old
        assert items[0]['price_usd'] == 101.1234 and items[0]['quote_date'] == '2026-11-06'
        assert any('AMZN' in w and 'skipped' in w for w in response.json()['warnings'])
    assert [r.url.params['symbol'] for r in stock_calls(feeds)] == ['NCNO']


@pytest.mark.parametrize(('message', 'kind'), [
    ('Your API key is valid but the supplied symbol is invalid.', 'unknown'),
    ('Invalid API call. Visit the documentation for details about your API key.', 'unknown'),
    ('Your API key is invalid.', 'auth'),
    ('Invalid or missing API key.', 'auth'),
    ('This premium endpoint allows 100 requests per day with a subscription.', 'premium'),
    ('Our standard API rate limit is 25 requests per day; upgrade to premium.', 'quota'),
    ('Our free API service supports up to 25 requests per day. Subscribe to premium.', 'quota'),
])
def test_notice_classifier_does_not_guess(message, kind):
    assert trading._classify_stock_notice(message) == kind


def test_unknown_error_message_stops_but_invalid_symbol_error_does_not(client, feeds):
    client.put('/api/market-data', json={'api_key': 'mock-key'})
    client.put('/api/settings', json={'market_mode': 'stocks', 'stock_symbols': ['AAPL', 'NCNO', 'AMZN']})
    feeds.stock['AAPL'] = {'Error Message': 'Invalid symbol'}
    feeds.stock['NCNO'] = {'Error Message': 'Account is temporarily unavailable'}
    response = client.post('/api/scan')
    assert response.status_code == 502
    assert [r.url.params['symbol'] for r in stock_calls(feeds)] == ['AAPL', 'NCNO']
    assert 'cause is unknown' in response.text and 'AMZN' in response.text and 'skipped' in response.text
    assert 'temporarily unavailable' not in response.text


def test_fresh_cache_reused_even_if_credential_store_unavailable(client, feeds, monkeypatch):
    client.put('/api/market-data', json={'api_key': 'mock-secret'})
    client.put('/api/settings', json={'market_mode': 'stocks', 'stock_symbols': ['AAPL']})
    first = client.post('/api/scan').json()['candidates'][0]
    monkeypatch.setenv('APP_ENCRYPTION_KEY', Fernet.generate_key().decode())
    feeds.calls.clear()
    response = client.post('/api/scan')
    assert response.status_code == 200 and not stock_calls(feeds)
    cached = response.json()['candidates'][0]
    assert cached['cache_status'] == 'cached' and cached['observed_at'] == first['observed_at']
    assert any('only cached' in w.lower() for w in response.json()['warnings'])


def test_watchlist_change_during_stock_scan_does_not_reintroduce_removed_symbol(client, feeds, monkeypatch):
    client.put('/api/market-data', json={'api_key': 'mock-key'})
    client.put('/api/settings', json={'market_mode': 'stocks', 'stock_symbols': ['AAPL']})
    original = trading._scan_stocks
    async def change_watchlist(symbols):
        result = await original(symbols)
        trading.update_settings({'stock_symbols': ['NCNO']})
        return result
    monkeypatch.setattr(trading, '_scan_stocks', change_watchlist)
    response = client.post('/api/scan')
    assert response.status_code == 502 and 'changed during scan' in response.text
    assert client.get('/api/dashboard').json()['candidates'] == []
    with db.read_connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM stock_quote_cache').fetchone()[0] == 0


def test_cache_provider_namespaces_prevent_cross_source_prices(client, feeds):
    client.put('/api/market-data', json={'api_key': 'mock-key'})
    client.put('/api/settings', json={'market_mode': 'stocks', 'stock_symbols': ['AAPL']})
    observed = db.utc_now()
    other = {'symbol': 'AAPL', 'price_usd': '999', 'quote_date': '2026-11-06',
             'stock_volume_shares': 100, 'stock_change_percent': None, 'observed_at': observed}
    with db.transaction() as conn:
        db.put_stock_quote_cache(conn, provider='other_provider:quote', item=other)
    result = client.post('/api/scan').json()['candidates'][0]
    assert result['price_usd'] == 178.1 and result['source'] == 'Alpha Vantage'
    assert result['cache_status'] == 'fresh' and len(stock_calls(feeds)) == 1
    with db.read_connection() as conn:
        alpha = db.get_stock_quote_cache(conn, provider=db.ALPHA_VANTAGE_QUOTE_CACHE, symbols=['AAPL'])
        other_cache = db.get_stock_quote_cache(conn, provider='other_provider:quote', symbols=['AAPL'])
        assert alpha['AAPL']['price_usd'] == '178.1000' and other_cache['AAPL']['price_usd'] == '999'
    age_stock_cache(symbols=['AAPL'])
    feeds.stock['AAPL'] = {'Information': 'rate limit'}
    result = client.post('/api/scan').json()['candidates'][0]
    assert result['cache_status'] == 'stale' and result['source'] == 'Alpha Vantage' and result['price_usd'] == 178.1


def test_migration_from_unnamespaced_cache_preserves_quote(tmp_path, monkeypatch):
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    db.init_db()
    observed = db.utc_now()
    with sqlite3.connect(db.data_path()) as conn:
        conn.execute('DROP TABLE stock_quote_cache')
        conn.execute('''CREATE TABLE stock_quote_cache(symbol TEXT PRIMARY KEY, price_usd TEXT NOT NULL,
            quote_date TEXT NOT NULL, stock_volume_shares INTEGER NOT NULL,
            stock_change_percent TEXT, observed_at TEXT NOT NULL)''')
        conn.execute('INSERT INTO stock_quote_cache VALUES (?,?,?,?,?,?)',
                     ('AAPL', '34.56', '2026-11-06', 20, '-1.2', observed))
    db.init_db()
    db.init_db()
    with db.read_connection() as conn:
        row = db.get_stock_quote_cache(conn, provider=db.ALPHA_VANTAGE_QUOTE_CACHE, symbols=['AAPL'])['AAPL']
        assert row['price_usd'] == '34.56' and row['observed_at'] == observed
        assert db.get_stock_quote_cache(conn, provider='other_provider:quote', symbols=['AAPL']) == {}
