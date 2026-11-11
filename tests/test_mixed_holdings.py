"""Mixed paper holdings through the real API and SQLite ledger, without network access."""
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import socket
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app


D = Decimal
START = D("25000")
SLIPPAGE_BPS = D("25")
FEE_BPS = D("15")
STOCK_QTY, STOCK_PRICE, STOCK_MARK = D("12.5"), D("100.20"), D("110.45")
CRYPTO_QTY, CRYPTO_PRICE, CRYPTO_MARK = D("0.75"), D("3000"), D("3200.10")


@pytest.fixture
def isolated_dir(tmp_path, monkeypatch):
    # Authentication is disabled only by tests/conftest.py, not by this fixture.
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    attempted = []
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def no_dns(*args, **kwargs):
        attempted.append(("dns", args))
        raise AssertionError("Unexpected network lookup")

    def no_connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            attempted.append(("connect", address))
            raise AssertionError("Unexpected network connection")
        return original_connect(sock, address)

    def no_connect_ex(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            attempted.append(("connect_ex", address))
            raise AssertionError("Unexpected network connection")
        return original_connect_ex(sock, address)

    monkeypatch.setattr(socket, "getaddrinfo", no_dns)
    monkeypatch.setattr(socket.socket, "connect", no_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", no_connect_ex)
    yield tmp_path
    # Even provider code that catches the network exception cannot hide an attempt.
    assert not attempted, f"External network attempted: {attempted!r}"


@pytest.fixture
def client(isolated_dir):
    with TestClient(app) as test_client:
        yield test_client


def ok(response):
    assert response.status_code == 200, response.text
    return response.json()


def close(actual, expected):
    assert actual == pytest.approx(float(expected), rel=1e-12, abs=1e-9)


def buy_cost(quantity, price):
    fill = price * (1 + SLIPPAGE_BPS / 10000)
    fee = quantity * fill * FEE_BPS / 10000
    return fill, fee, quantity * fill + fee


def sell_proceeds(quantity, price):
    fill = price * (1 - SLIPPAGE_BPS / 10000)
    fee = quantity * fill * FEE_BPS / 10000
    return fill, fee, quantity * fill - fee


def configure(client, mode="both"):
    settings = ok(client.put("/api/settings", json={
        "starting_balance": str(START), "slippage_bps": str(SLIPPAGE_BPS),
        "fee_bps": str(FEE_BPS), "market_mode": mode, "auto_paper": False,
    }))
    assert settings["market_mode"] == mode
    close(settings["slippage_bps"], SLIPPAGE_BPS)
    close(settings["fee_bps"], FEE_BPS)


def trade(client, symbol, asset_type, side, quantity, price, reason):
    result = ok(client.post("/api/trades", json={
        "symbol": symbol, "asset_type": asset_type, "side": side,
        "quantity": str(quantity), "price": str(price), "reason": reason,
    }))
    assert (result["symbol"], result["asset_type"], result["side"], result["reason"]) == (
        symbol, asset_type, side, reason)
    close(result["quantity"], quantity)
    close(result["reference_price"], price)
    close(result["slippage_bps"], SLIPPAGE_BPS)
    fill, fee, _ = (buy_cost if side == "BUY" else sell_proceeds)(quantity, price)
    close(result["fill_price"], fill)
    close(result["fee"], fee)
    return result


def positions(data):
    items = data["positions"]
    keyed = {(p["symbol"], p["asset_type"]): p for p in items}
    assert len(keyed) == len(items), "Duplicate symbol/asset-type positions"
    assert data["summary"]["open_positions"] == len(items)
    return keyed


def mark(client, position, price):
    result = ok(client.patch(f"/api/positions/{position['id']}/mark", json={"price": str(price)}))
    assert result["id"] == position["id"] and result["symbol"] == position["symbol"]
    assert result["mark_source"] == "manual" and result["mark_timestamp"]
    close(result["mark_price"], price)


def assert_summary(data, cash, equity, unrealized, realized=0, closed=0):
    summary = data["summary"]
    close(summary["cash"], cash)
    if equity is None:
        assert summary["equity"] is None and summary["unrealized_pnl"] is None
    else:
        close(summary["equity"], equity)
        close(summary["unrealized_pnl"], unrealized)
    close(summary["realized_pnl"], realized)
    assert summary["closed_trades"] == closed


@pytest.mark.parametrize("initial_mode", ["stocks", "crypto", "both"])
def test_mixed_buys_and_marks_are_independent_of_scan_mode(client, initial_mode):
    configure(client, initial_mode)
    stock_buy = trade(client, "AAPL", "stock", "BUY", STOCK_QTY, STOCK_PRICE, "buy shares")
    crypto_buy = trade(client, "BTC", "crypto", "BUY", CRYPTO_QTY, CRYPTO_PRICE, "buy coins")
    assert stock_buy["realized_pnl"] is None and crypto_buy["realized_pnl"] is None
    _, _, stock_basis = buy_cost(STOCK_QTY, STOCK_PRICE)
    _, _, crypto_basis = buy_cost(CRYPTO_QTY, CRYPTO_PRICE)
    cash = START - stock_basis - crypto_basis

    data = ok(client.get("/api/dashboard"))
    held = positions(data)
    assert set(held) == {("AAPL", "stock"), ("BTC", "crypto")}
    assert Counter(p["asset_type"] for p in held.values()) == {"stock": 1, "crypto": 1}
    for key, qty, basis in [(('AAPL', 'stock'), STOCK_QTY, stock_basis),
                            (('BTC', 'crypto'), CRYPTO_QTY, crypto_basis)]:
        close(held[key]["quantity"], qty)
        close(held[key]["entry_price"], basis / qty)
        assert held[key]["mark_price"] is None
        assert held[key]["mark_source"] is None and held[key]["unrealized_pnl"] is None
    assert_summary(data, cash, None, None)

    mark(client, held[("BTC", "crypto")], CRYPTO_MARK)
    partially_marked = ok(client.get("/api/dashboard"))
    partial_positions = positions(partially_marked)
    assert partial_positions[("AAPL", "stock")]["mark_price"] is None
    close(partial_positions[("BTC", "crypto")]["unrealized_pnl"], CRYPTO_QTY * CRYPTO_MARK - crypto_basis)
    assert_summary(partially_marked, cash, None, None)  # even one missing stock mark
    mark(client, held[("AAPL", "stock")], STOCK_MARK)

    stock_unrealized = STOCK_QTY * STOCK_MARK - stock_basis
    crypto_unrealized = CRYPTO_QTY * CRYPTO_MARK - crypto_basis
    equity = cash + STOCK_QTY * STOCK_MARK + CRYPTO_QTY * CRYPTO_MARK
    for mode in ("stocks", "crypto", "both"):
        ok(client.put("/api/settings", json={"market_mode": mode}))
        data = ok(client.get("/api/dashboard"))
        assert data["settings"]["market_mode"] == mode
        held = positions(data)
        assert set(held) == {("AAPL", "stock"), ("BTC", "crypto")}
        assert Counter(p["asset_type"] for p in held.values()) == {"stock": 1, "crypto": 1}
        for key, qty, price, unrealized in [
            (("AAPL", "stock"), STOCK_QTY, STOCK_MARK, stock_unrealized),
            (("BTC", "crypto"), CRYPTO_QTY, CRYPTO_MARK, crypto_unrealized),
        ]:
            p = held[key]
            close(p["quantity"], qty)
            close(p["mark_price"], price)
            close(p["unrealized_pnl"], unrealized)
            assert p["mark_source"] == "manual" and p["mark_timestamp"]
        assert_summary(data, cash, equity, stock_unrealized + crypto_unrealized)
        assert [(t["id"], t["symbol"], t["asset_type"], t["side"]) for t in data["trades"]] == [
            (crypto_buy["id"], "BTC", "crypto", "BUY"), (stock_buy["id"], "AAPL", "stock", "BUY")]
        for saved, returned in zip(data["trades"], [crypto_buy, stock_buy]):
            assert saved == returned
        close(data["equity_curve"][-1]["equity"], equity)


def test_partial_sells_close_one_asset_and_preserve_other_and_history(client):
    configure(client)
    trades = [trade(client, "AAPL", "stock", "BUY", STOCK_QTY, STOCK_PRICE, "stock buy"),
              trade(client, "BTC", "crypto", "BUY", CRYPTO_QTY, CRYPTO_PRICE, "crypto buy")]
    held = positions(ok(client.get("/api/dashboard")))
    mark(client, held[("AAPL", "stock")], STOCK_MARK)
    mark(client, held[("BTC", "crypto")], CRYPTO_MARK)
    _, _, stock_basis = buy_cost(STOCK_QTY, STOCK_PRICE)
    _, _, crypto_basis = buy_cost(CRYPTO_QTY, CRYPTO_PRICE)
    cash = START - stock_basis - crypto_basis
    realized = D(0)

    # A partial sale of each asset allocates only the sold fraction of its own basis.
    for symbol, asset, sell_qty, sell_price, held_qty, basis in [
        ("AAPL", "stock", D("2.5"), D("120"), STOCK_QTY, stock_basis),
        ("BTC", "crypto", D("0.25"), D("3300"), CRYPTO_QTY, crypto_basis),
    ]:
        sold = trade(client, symbol, asset, "SELL", sell_qty, sell_price, f"partial {asset}")
        _, _, net = sell_proceeds(sell_qty, sell_price)
        gain = net - basis * sell_qty / held_qty
        close(sold["realized_pnl"], gain)
        cash += net
        realized += gain
        trades.append(sold)
    stock_remaining = STOCK_QTY - D("2.5")
    crypto_remaining = CRYPTO_QTY - D("0.25")
    stock_basis_remaining = stock_basis * stock_remaining / STOCK_QTY
    crypto_basis_remaining = crypto_basis * crypto_remaining / CRYPTO_QTY
    data = ok(client.get("/api/dashboard"))
    held = positions(data)
    assert set(held) == {("AAPL", "stock"), ("BTC", "crypto")}
    close(held[("AAPL", "stock")]["quantity"], stock_remaining)
    close(held[("BTC", "crypto")]["quantity"], crypto_remaining)
    unrealized = (stock_remaining * STOCK_MARK - stock_basis_remaining
                  + crypto_remaining * CRYPTO_MARK - crypto_basis_remaining)
    assert_summary(data, cash, cash + stock_remaining * STOCK_MARK + crypto_remaining * CRYPTO_MARK,
                   unrealized, realized, 2)
    assert data["summary"]["win_rate"] == 100

    # Close only the stock, while viewing crypto mode; the crypto position stays intact.
    ok(client.put("/api/settings", json={"market_mode": "crypto"}))
    sold = trade(client, "AAPL", "stock", "SELL", stock_remaining, D("95"), "close stock")
    _, _, net = sell_proceeds(stock_remaining, D("95"))
    gain = net - stock_basis_remaining
    close(sold["realized_pnl"], gain)
    cash += net
    realized += gain
    trades.append(sold)
    data = ok(client.get("/api/dashboard"))
    held = positions(data)
    assert set(held) == {("BTC", "crypto")}
    assert Counter(p["asset_type"] for p in held.values()) == {"crypto": 1}
    close(held[("BTC", "crypto")]["quantity"], crypto_remaining)
    close(held[("BTC", "crypto")]["entry_price"], crypto_basis_remaining / crypto_remaining)
    close(held[("BTC", "crypto")]["mark_price"], CRYPTO_MARK)
    assert_summary(data, cash, cash + crypto_remaining * CRYPTO_MARK,
                   crypto_remaining * CRYPTO_MARK - crypto_basis_remaining, realized, 3)
    close(data["summary"]["win_rate"], D(200) / 3)

    # Closing the final holding makes equity equal cash; all executions remain in history.
    sold = trade(client, "BTC", "crypto", "SELL", crypto_remaining, D("3100"), "close crypto")
    _, _, net = sell_proceeds(crypto_remaining, D("3100"))
    gain = net - crypto_basis_remaining
    close(sold["realized_pnl"], gain)
    cash += net
    realized += gain
    trades.append(sold)
    data = ok(client.get("/api/dashboard"))
    assert positions(data) == {}
    assert_summary(data, cash, cash, 0, realized, 4)
    close(data["summary"]["win_rate"], D(75))
    assert data["trades"] == list(reversed(trades))
    close(data["equity_curve"][-1]["equity"], cash)


def test_same_symbol_is_separate_by_asset_type_for_buys_marks_and_sells(client):
    configure(client, "stocks")
    first_stock = trade(client, "ABC", "stock", "BUY", D("5"), D("40"), "shares one")
    crypto = trade(client, "ABC", "crypto", "BUY", D("2"), D("10"), "coins")
    second_stock = trade(client, "ABC", "stock", "BUY", D("3"), D("60"), "shares two")
    data = ok(client.get("/api/dashboard"))
    held = positions(data)
    assert set(held) == {("ABC", "stock"), ("ABC", "crypto")}
    assert held[("ABC", "stock")]["id"] != held[("ABC", "crypto")]["id"]
    close(held[("ABC", "stock")]["quantity"], D("8"))
    close(held[("ABC", "crypto")]["quantity"], D("2"))
    _, _, basis1 = buy_cost(D("5"), D("40"))
    _, _, basis2 = buy_cost(D("3"), D("60"))
    _, _, crypto_basis = buy_cost(D("2"), D("10"))
    close(held[("ABC", "stock")]["entry_price"], (basis1 + basis2) / D("8"))
    close(held[("ABC", "crypto")]["entry_price"], crypto_basis / D("2"))
    mark(client, held[("ABC", "stock")], D("50"))
    mark(client, held[("ABC", "crypto")], D("12"))
    before = ok(client.get("/api/dashboard"))
    stock_id = positions(before)[("ABC", "stock")]["id"]
    cash = START - basis1 - basis2 - crypto_basis
    assert_summary(before, cash, cash + D("8") * D("50") + D("2") * D("12"),
                   D("8") * D("50") - basis1 - basis2 + D("2") * D("12") - crypto_basis)

    # An oversell of crypto must not borrow the stock quantity with the same ticker.
    rejected = client.post("/api/trades", json={"symbol": "ABC", "asset_type": "crypto", "side": "SELL",
                                                   "quantity": "3", "price": "11", "reason": "too many coins"})
    assert rejected.status_code == 409
    after_rejected = ok(client.get("/api/dashboard"))
    assert after_rejected["positions"] == before["positions"]
    assert after_rejected["summary"] == before["summary"]
    assert after_rejected["trades"] == before["trades"]

    sold = trade(client, "ABC", "crypto", "SELL", D("2"), D("11"), "close coins")
    _, _, proceeds = sell_proceeds(D("2"), D("11"))
    close(sold["realized_pnl"], proceeds - crypto_basis)
    after = ok(client.get("/api/dashboard"))
    held = positions(after)
    assert set(held) == {("ABC", "stock")}
    assert held[("ABC", "stock")]["id"] == stock_id
    close(held[("ABC", "stock")]["quantity"], D("8"))
    close(held[("ABC", "stock")]["mark_price"], D("50"))
    assert_summary(after, cash + proceeds, cash + proceeds + D("8") * D("50"),
                   D("8") * D("50") - basis1 - basis2, proceeds - crypto_basis, 1)
    assert after["trades"] == [sold, second_stock, crypto, first_stock]


def test_cached_watchlist_scans_and_imports_never_create_phantom_holdings(client):
    configure(client, "stocks")
    ok(client.put("/api/settings", json={"stock_source_mode": "both", "stock_symbols": ["AAPL", "MSFT"]}))
    observed = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
    # Seed provider cache as data, not a mock of the scanner or accounting.
    with db.transaction() as conn:
        for symbol, price in [("AAPL", "999"), ("MSFT", "33")]:
            db.put_stock_quote_cache(conn, provider=db.ALPHA_VANTAGE_QUOTE_CACHE, item={
                "symbol": symbol, "price_usd": price, "quote_date": "2026-11-06",
                "stock_volume_shares": 1000, "stock_change_percent": None, "observed_at": observed,
            })
    scan = ok(client.post("/api/scan"))
    assert [(c["symbol"], c["asset_type"], c["cache_status"]) for c in scan["candidates"]] == [
        ("AAPL", "stock", "cached"), ("MSFT", "stock", "cached")]
    data = ok(client.get("/api/dashboard"))
    assert positions(data) == {} and data["trades"] == []
    assert_summary(data, START, START, 0)

    trade(client, "AAPL", "stock", "BUY", D("2"), D("100"), "buy watched stock")
    trade(client, "BTC", "crypto", "BUY", D("1"), D("500"), "buy unwatched coin")
    data = ok(client.get("/api/dashboard"))
    held = positions(data)
    mark(client, held[("BTC", "crypto")], D("600"))
    before_import = ok(client.get("/api/dashboard"))
    before_held = positions(before_import)
    assert ok(client.post("/api/stock-import", json={"csv": "Symbol,Price\nAAPL,1234\nTSLA,50\n"}))["imported"] == 2
    cycle = ok(client.post("/api/cycle"))
    assert cycle["auto_paper"] is False and cycle["trades"] == []
    assert [(c["symbol"], c["source"], c["price_usd"], c["cache_status"]) for c in cycle["candidates"]] == [
        ("AAPL", "Alpha Vantage", 999, "cached"), ("MSFT", "Alpha Vantage", 33, "cached"),
        ("AAPL", "User CSV import", 1234, "manual"), ("TSLA", "User CSV import", 50, "manual")]
    data = ok(client.get("/api/dashboard"))
    held = positions(data)
    assert set(held) == {("AAPL", "stock"), ("BTC", "crypto")}
    assert {c["symbol"] for c in data["candidates"]} == {"AAPL", "MSFT", "TSLA"}
    assert data["candidates"] == cycle["candidates"]
    assert held == before_held
    assert data["trades"] == before_import["trades"] and data["summary"] == before_import["summary"]
    assert held[("AAPL", "stock")]["mark_price"] is None
    assert held[("AAPL", "stock")]["mark_source"] is None
    close(held[("BTC", "crypto")]["mark_price"], D("600"))
    _, _, stock_basis = buy_cost(D("2"), D("100"))
    _, _, crypto_basis = buy_cost(D("1"), D("500"))
    assert_summary(data, START - stock_basis - crypto_basis, None, None)
    assert len(data["trades"]) == 2

    # Removing the stock from the watchlist removes only its API candidate, not its CSV quote or holding.
    ok(client.put("/api/settings", json={"stock_symbols": ["MSFT"]}))
    scan_after_removal = ok(client.post("/api/scan"))
    assert [(c["symbol"], c["source"], c["price_usd"], c["cache_status"]) for c in scan_after_removal["candidates"]] == [
        ("MSFT", "Alpha Vantage", 33, "cached"), ("AAPL", "User CSV import", 1234, "manual"),
        ("TSLA", "User CSV import", 50, "manual")]
    after = ok(client.get("/api/dashboard"))
    assert positions(after) == held
    assert after["trades"] == data["trades"] and after["summary"] == data["summary"]
    assert any(c["symbol"] == "AAPL" and c["cache_status"] == "manual" for c in after["candidates"])
    assert after["candidates"] == scan_after_removal["candidates"]


def test_legacy_ledger_initialization_reads_and_updates_mixed_holdings(isolated_dir):
    # Schema mirrors the pre-market-mode ledger: candidate asset metadata is absent,
    # while positions and trade history already use the (symbol, asset_type) key.
    timestamp = "2020-01-01T00:00:00Z"
    path = isolated_dir / "tradeledger.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE settings (id INTEGER PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE account (id INTEGER PRIMARY KEY, cash TEXT NOT NULL);
            CREATE TABLE positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, asset_type TEXT NOT NULL,
                quantity TEXT NOT NULL, cost_basis TEXT NOT NULL, mark_price TEXT,
                mark_source TEXT, mark_timestamp TEXT, chain TEXT, pair_address TEXT,
                opened_at TEXT NOT NULL, UNIQUE(symbol, asset_type));
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, symbol TEXT NOT NULL,
                side TEXT NOT NULL, asset_type TEXT NOT NULL, quantity TEXT NOT NULL,
                reference_price TEXT NOT NULL, fill_price TEXT NOT NULL, slippage_bps TEXT NOT NULL,
                fee TEXT NOT NULL, realized_pnl TEXT, reason TEXT NOT NULL, chain TEXT, pair_address TEXT);
            CREATE TABLE candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, chain TEXT NOT NULL,
                pair_address TEXT NOT NULL, price_usd TEXT, liquidity_usd TEXT, volume_24h TEXT,
                price_change_24h TEXT, risk_status TEXT NOT NULL, reason TEXT NOT NULL,
                url TEXT, observed_at TEXT NOT NULL);
        """)
        conn.execute("INSERT INTO settings VALUES (1, ?)", (json.dumps({"fee_bps": 0, "slippage_bps": 0}),))
        conn.execute("INSERT INTO account VALUES (1, '99630')")
        conn.executemany("""INSERT INTO positions(id,symbol,asset_type,quantity,cost_basis,mark_price,
                            mark_source,mark_timestamp,opened_at) VALUES(?,?,?,?,?,?,?,?,?)""", [
            (11, "ABC", "stock", "3", "270", "110", "manual", timestamp, timestamp),
            (12, "ABC", "crypto", "2", "100", "60", "manual", timestamp, timestamp),
        ])
        conn.executemany("""INSERT INTO trades(id,timestamp,symbol,side,asset_type,quantity,reference_price,
                            fill_price,slippage_bps,fee,realized_pnl,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", [
            (21, timestamp, "ABC", "BUY", "stock", "3", "90", "90", "0", "0", None, "old shares"),
            (22, timestamp, "ABC", "BUY", "crypto", "2", "50", "50", "0", "0", None, "old coins"),
        ])
        conn.execute("""INSERT INTO candidates(symbol,chain,pair_address,price_usd,risk_status,reason,observed_at)
                        VALUES('FOO','solana','pair1','2','eligible','legacy candidate',?)""", (timestamp,))

    with TestClient(app) as migrated:
        data = ok(migrated.get("/api/dashboard"))
        assert data["settings"]["market_mode"] == "crypto" and data["settings"]["stock_symbols"] == ["IBM"]
        held = positions(data)
        assert set(held) == {("ABC", "stock"), ("ABC", "crypto")}
        assert held[("ABC", "stock")]["id"] == 11 and held[("ABC", "crypto")]["id"] == 12
        close(held[("ABC", "stock")]["quantity"], D("3"))
        close(held[("ABC", "crypto")]["quantity"], D("2"))
        assert_summary(data, D("99630"), D("100080"), D("80"))
        assert [(t["id"], t["asset_type"], t["reason"]) for t in data["trades"]] == [
            (22, "crypto", "old coins"), (21, "stock", "old shares")]
        assert [(c["symbol"], c["asset_type"], c["source"]) for c in data["candidates"]] == [
            ("FOO", "crypto", "DexScreener")]
        for mode in ("stocks", "both"):
            ok(migrated.put("/api/settings", json={"market_mode": mode}))
            assert positions(ok(migrated.get("/api/dashboard"))) == held
        sale = ok(migrated.post("/api/trades", json={"symbol": "ABC", "asset_type": "stock", "side": "SELL",
                                                       "quantity": "1", "price": "120", "reason": "post migration"}))
        assert sale["asset_type"] == "stock" and sale["id"] > 22
        close(sale["realized_pnl"], D("30"))
        updated = ok(migrated.get("/api/dashboard"))
        updated_held = positions(updated)
        close(updated_held[("ABC", "stock")]["quantity"], D("2"))
        close(updated_held[("ABC", "crypto")]["quantity"], D("2"))
        assert_summary(updated, D("99750"), D("100090"), D("60"), D("30"), 1)
        assert [t["id"] for t in updated["trades"]] == [sale["id"], 22, 21]

    # Exercise the initializer again as a restart would, proving migration idempotence.
    db._initialized_paths.discard(str(path.absolute()))
    with TestClient(app) as restarted:
        after_restart = ok(restarted.get("/api/dashboard"))
        assert positions(after_restart) == updated_held
        assert after_restart["trades"] == updated["trades"]
        assert after_restart["summary"] == updated["summary"]
