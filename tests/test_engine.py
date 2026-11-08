import concurrent.futures
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app import db, trading


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    with TestClient(app) as client:
        yield client


def order(symbol="AAPL", asset_type="stock", side="BUY", quantity=10, price=100, reason="Manual thesis"):
    return dict(symbol=symbol, asset_type=asset_type, side=side, quantity=quantity, price=price, reason=reason)


def test_initial_state_is_real_and_empty(client):
    result = client.get("/api/dashboard").json()
    assert result["settings"]["auto_paper"] is False
    assert result["summary"]["cash"] == 100000
    assert result["summary"]["equity"] == 100000
    assert result["positions"] == result["candidates"] == result["trades"] == []
    assert len(result["equity_curve"]) == 1
    assert client.get("/api/health").json()["continuous_worker"] == "off"


def test_accounting_fees_slippage_marks_and_partial_sells(client):
    buy = client.post("/api/trades", json=order()).json()
    assert buy["fill_price"] == pytest.approx(100.1)
    assert buy["fee"] == pytest.approx(1.001)
    data = client.get("/api/dashboard").json()
    assert data["summary"]["cash"] == pytest.approx(98997.999)
    assert data["summary"]["equity"] is None
    assert data["positions"][0]["mark_price"] is None
    assert data["positions"][0]["entry_price"] == pytest.approx(100.2001)
    pid = data["positions"][0]["id"]
    assert client.patch(f"/api/positions/{pid}/mark", json={"price": 110}).status_code == 200
    data = client.get("/api/dashboard").json()
    assert data["summary"]["equity"] == pytest.approx(100097.999)
    assert data["summary"]["unrealized_pnl"] == pytest.approx(97.999)
    sold = client.post("/api/trades", json=order(side="SELL", quantity=5, price=120)).json()
    expected = Decimal("5") * Decimal("119.88") * Decimal("0.999") - Decimal("5") * Decimal("100.2001")
    assert sold["realized_pnl"] == pytest.approx(float(expected))
    data = client.get("/api/dashboard").json()
    assert data["positions"][0]["quantity"] == 5
    assert data["summary"]["realized_pnl"] == pytest.approx(float(expected))
    assert data["summary"]["closed_trades"] == 1
    assert data["summary"]["win_rate"] == 100
    assert client.post("/api/trades", json=order(side="SELL", quantity=6)).status_code == 409
    assert client.get("/api/dashboard").json()["positions"][0]["quantity"] == 5


def test_crypto_and_no_short(client):
    assert client.post("/api/trades", json=order("BTC", "crypto", "SELL", 1, 10)).status_code == 409
    assert client.post("/api/trades", json=order("BTC", "crypto", "BUY", .25, 10)).status_code == 200
    assert client.post("/api/trades", json=order("BTC", "crypto", "SELL", .25, 10)).status_code == 200
    data = client.get("/api/dashboard").json()
    assert data["positions"] == []
    assert data["summary"]["equity"] == data["summary"]["cash"]
    assert data["summary"]["realized_pnl"] < 0


def test_validation_and_origin_policy(client):
    for patch in ({"auto_paper": "true"}, {"risk_percent": -1}, {"fee_bps": 10001}, {"starting_balance": None}, {"unknown": 1}):
        assert client.put("/api/settings", json=patch).status_code == 422
    assert client.post("/api/trades", json=order(quantity=0)).status_code == 422
    assert client.post("/api/trades", json=order(price=-1)).status_code == 422
    assert client.post("/api/trades", json=order(reason="\n\r")).status_code == 422
    assert client.post("/api/trades", content='{"symbol":"AAPL","asset_type":"stock","side":"BUY","quantity":1,"price":NaN,"reason":"x"}', headers={"content-type": "application/json"}).status_code == 422
    assert client.put("/api/settings", content='{"risk_percent":Infinity}', headers={"content-type": "application/json"}).status_code == 422
    assert client.post("/api/trades", json=order(), headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/trades", json=order(), headers={"Origin": "http://testserver"}).status_code == 200
    assert client.put("/api/settings", json={"starting_balance": 500}).status_code == 409
    assert client.get("/api/dashboard").json()["summary"]["open_positions"] == 1


def test_concurrent_orders_cannot_overspend(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    db.ensure_initialized()
    def buy(i):
        try:
            return trading.manual_trade(f"TEST{i}", "stock", "BUY", Decimal("1"), Decimal("75000"), "race test")
        except trading.TradeError:
            return None
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(buy, range(8)))
    assert sum(x is not None for x in results) == 1
    data = trading.dashboard()
    assert len(data["trades"]) == 1
    assert data["positions"][0]["quantity"] == 1
    assert data["summary"]["cash"] >= 0


def test_concurrent_identical_orders_are_deduplicated(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    db.ensure_initialized()
    def buy(_):
        try:
            return trading.manual_trade("AAPL", "stock", "BUY", Decimal("1"), Decimal("10"), "same submission")
        except trading.TradeError:
            return None
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(buy, range(8)))
    assert sum(item is not None for item in results) == 1
    assert len(trading.dashboard()["trades"]) == 1


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status
    def raise_for_status(self):
        if self.status >= 400:
            req = httpx.Request("GET", "https://api.dexscreener.com/")
            raise httpx.HTTPStatusError("upstream failure", request=req, response=httpx.Response(self.status, request=req))
    def json(self):
        return self.payload


@pytest.fixture
def market(monkeypatch):
    class Client:
        fail = False
        pairs = [
            {"chainId": "solana", "pairAddress": "pair123", "baseToken": {"address": "TokenAAA", "symbol": "FOO"}, "priceUsd": "2", "liquidity": {"usd": 100000}, "volume": {"h1": 10000, "h24": 33000}, "priceChange": {"h24": 3}, "pairCreatedAt": 1700000000000, "url": "https://dexscreener.com/solana/pair123"},
            {"chainId": "solana", "pairAddress": "pair456", "baseToken": {"address": "TokenBBB", "symbol": "BAR"}, "priceUsd": "1", "liquidity": {"usd": 90000}, "volume": {"h1": 10000}, "url": "javascript:alert(1)"},
            # A quote-token match must not be treated as its base-token price.
            {"chainId": "solana", "pairAddress": "pair789", "baseToken": {"address": "other", "symbol": "WRONG"}, "quoteToken": {"address": "TokenCCC"}, "priceUsd": "99", "liquidity": {"usd": 999999}, "volume": {"h24": 999999}}
        ]
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, url):
            if self.fail: return FakeResponse(None, 503)
            if "token-profiles" in url:
                return FakeResponse([{"chainId":"solana","tokenAddress": x} for x in ("TokenAAA", "TokenBBB", "TokenCCC")])
            return FakeResponse({"pairs": self.pairs})
    monkeypatch.setattr(trading.httpx, "AsyncClient", Client)
    return Client


def test_scan_and_cycle_fail_closed(client, market):
    scan = client.post("/api/scan")
    assert scan.status_code == 200
    candidates = scan.json()["candidates"]
    assert len(candidates) == 2
    assert candidates[0]["risk_status"] == "eligible"
    assert candidates[0]["volume_spike_ratio"] == pytest.approx(10)
    assert candidates[0]["pair_created_at"] == "2023-11-14T22:13:20Z"
    assert candidates[1]["risk_status"] == "filtered"
    assert candidates[1]["volume_spike_ratio"] is None
    assert candidates[1]["url"] is None
    assert client.post("/api/cycle").json()["trades"] == []
    assert client.put("/api/settings", json={"auto_paper": True}).status_code == 200
    cycle = client.post("/api/cycle").json()
    assert len(cycle["trades"]) == 1
    assert cycle["trades"][0]["symbol"] == "FOO"
    assert client.get("/api/dashboard").json()["positions"][0]["mark_source"] == "dexscreener"
    assert client.post("/api/cycle").json()["trades"] == []  # no repeat buy
    market.fail = True
    assert client.post("/api/cycle").status_code == 502
    data = client.get("/api/dashboard").json()
    assert len(data["trades"]) == 1
    assert next(a for a in data["agents"] if a["id"] == "scanner")["status"] == "error"


def test_automatic_sizing_rejects_unmarked_portfolio(client, market):
    assert client.post("/api/trades", json=order()).status_code == 200
    assert client.put("/api/settings", json={"auto_paper": True}).status_code == 200
    data = client.post("/api/cycle").json()
    assert data["trades"] == []
    assert any("unmarked" in x["reason"] for x in data["skipped"])
    assert len(client.get("/api/dashboard").json()["trades"]) == 1
