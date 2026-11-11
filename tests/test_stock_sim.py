import concurrent.futures
import json
import socket
from decimal import Decimal

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import auth, db, stock_sim, trading


@pytest.fixture
def demo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    db.ensure_initialized()
    # Fail immediately if code under test attempts real network access.
    def no_network(*args, **kwargs):
        raise AssertionError("Offline simulation attempted network access")
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(httpx.AsyncClient, "request", no_network)
    monkeypatch.setattr(trading.market_data, "load_key", no_network)
    monkeypatch.setattr(trading, "_scan_stocks", no_network)
    app = FastAPI()
    app.include_router(stock_sim.router)
    with TestClient(app) as client:
        yield client


def set_config(**patch):
    with db.transaction() as conn:
        settings = db.get_settings(conn)
        settings.update(patch)
        conn.execute("UPDATE settings SET data=? WHERE id=1", (json.dumps(settings),))


def table_dump(names):
    with db.read_connection() as conn:
        return {name: [tuple(r) for r in conn.execute('SELECT * FROM "' + name + '" ORDER BY rowid')]
                for name in names}


def test_candidates_are_deterministic_unique_offline_and_disclosed():
    symbols = [chr(65 + n) for n in range(25)]
    observed = "2026-11-11T08:00:00Z"
    a = stock_sim.make_candidates(symbols, 7, observed)
    b = stock_sim.make_candidates(list(reversed(symbols)), 7, observed)
    assert len(a) == len({c["symbol"] for c in a}) == 25
    assert {c["symbol"]: c["price_usd"] for c in a} == {c["symbol"]: c["price_usd"] for c in b}
    assert len({c["price_usd"] for c in a}) == 25
    assert a == stock_sim.make_candidates(symbols, 7, observed)
    assert any(c["price_usd"] != d["price_usd"] for c, d in zip(a, stock_sim.make_candidates(symbols, 8, observed)))
    # These exact strings are usable by the existing candidate insertion path.
    for c in a:
        assert set(trading._CANDIDATE_FIELDS) <= set(c)
        assert Decimal("1") <= Decimal(c["price_usd"]) <= Decimal("1000")
        assert Decimal(c["price_usd"]).as_tuple().exponent >= -18
        assert c["source"] == "Simulation" and c["cache_status"] == "simulated"
        assert c["risk_status"] == "simulated"
        assert stock_sim.DISCLOSURE in c["reason"]
        assert c["observed_at"] == observed
        assert all(c[key] is None for key in ("quote_date", "quote_timestamp", "stock_volume_shares", "volume_24h", "liquidity_usd", "stock_change_percent", "price_change_24h"))
    for bad_symbols in (["BRK.B", "BRK.B"], ["bad/ticker"], ["A"] * 29, [None]):
        with pytest.raises(ValueError):
            stock_sim.make_candidates(bad_symbols, 1, observed)
    for step in (-1, stock_sim.MAX_STEP + 1, True, 1.5):
        with pytest.raises(ValueError):
            stock_sim.make_candidates(["IBM"], step, observed)
    with pytest.raises(ValueError):
        stock_sim.make_candidates(["IBM"], 1, "2026-01-01")


def test_disabled_guard_lazy_first_get_and_persistence(demo):
    assert stock_sim.current_step() == 0
    disabled = demo.get("/api/simulation/stocks").json()
    assert disabled["enabled"] is False and disabled["cash"] is None
    assert demo.post("/api/simulation/stocks/cycle").status_code == 409
    with db.read_connection() as conn:
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='stock_sim_account'").fetchone() is None
    # Provider alone is insufficient: only the explicit source mode opts in.
    set_config(stock_provider="simulation")
    assert demo.post("/api/simulation/stocks/cycle").status_code == 409
    set_config(stock_source_mode="simulation", stock_symbols=["AAPL", "MSFT", "IBM"])
    initial = demo.get("/api/simulation/stocks").json()
    assert initial["enabled"] is True and initial["step"] == 0
    assert initial["initial_cash"] == initial["cash"] == initial["equity"] == 100000
    assert initial["positions"] == initial["trades"] == []
    assert len(initial["equity_curve"]) == 1
    assert demo.get("/api/simulation/stocks").json()["step"] == 0
    advanced = demo.post("/api/simulation/stocks/cycle").json()
    assert advanced["step"] == 1 == stock_sim.current_step()
    assert advanced["summary"]["step"] == 1
    assert demo.get("/api/simulation/stocks").json()["step"] == 1
    set_config(stock_source_mode="api")
    persisted = demo.get("/api/simulation/stocks").json()
    assert persisted["enabled"] is False and persisted["step"] == 1
    assert persisted["cash"] == advanced["cash"] and persisted["positions"] == advanced["positions"]
    assert demo.post("/api/simulation/stocks/cycle").status_code == 409
    set_config(stock_source_mode="simulation")
    assert demo.get("/api/simulation/stocks").json()["step"] == 1


def test_cycles_account_correctly_and_leave_ledger_untouched(demo, monkeypatch):
    # Put manual holdings, marks and trades in the actual ledger before the demo.
    trading.manual_trade("IBM", "stock", "BUY", Decimal("4"), Decimal("100"), "Existing manual holding")
    with db.read_connection() as conn:
        position_id = conn.execute("SELECT id FROM positions WHERE symbol='IBM'").fetchone()[0]
    trading.mark_position(position_id, Decimal("105"))
    set_config(stock_source_mode="simulation", stock_symbols=["IBM", "AAPL", "MSFT", "ORCL", "TSLA"])
    names = ["account", "positions", "trades", "equity_snapshots", "activity", "candidates", "agents", "stock_quote_cache", "manual_stock_quotes", "settings"]
    before = table_dump(names)
    # No cache/minute cadence: every explicit click advances exactly one step.
    monkeypatch.setattr(db, "utc_now", lambda: "2026-11-11T08:00:00.123456Z")
    assert demo.get("/api/simulation/stocks").status_code == 200
    observed_sells = 0
    for step in range(1, 41):
        response = demo.post("/api/simulation/stocks/cycle")
        assert response.status_code == 200, response.text
        state = response.json()
        assert state["step"] == step
        assert state["summary"]["buys"] <= 1
        observed_sells += state["summary"]["sells"]
        assert state["cash"] >= 0 and state["equity"] >= 0
        assert len(state["positions"]) <= 3
        assert len({p["symbol"] for p in state["positions"]}) == len(state["positions"])
        assert all(p["mark_source"] == "Simulation" for p in state["positions"])
        assert all("Synthetic" in t["reason"] for t in state["trades"])
        assert state["equity"] == pytest.approx(state["cash"] + sum(p["quantity"] * p["mark_price"] for p in state["positions"]), abs=0.02)
        with db.read_connection() as conn:
            account = conn.execute("SELECT * FROM stock_sim_account").fetchone()
            assert Decimal(account["cash"]) >= 0
            buys = conn.execute("SELECT * FROM stock_sim_trades WHERE step=? AND side='BUY'", (step,)).fetchall()
            for trade in buys:
                price, q = Decimal(trade["price"]), trade["quantity"]
                cost = Decimal(trade["notional"]) + Decimal(trade["fee"])
                # The post-buy equity is pre-buy equity less the fee, rounded consistently.
                equity_before = Decimal(str(state["equity"])) + Decimal(trade["fee"])
                stop = stock_sim._money(Decimal(q) * price * Decimal("0.9"))
                estimate = cost - stop + stock_sim._money(stop * stock_sim.FEE_RATE)
                assert cost <= equity_before * Decimal("0.10")
                assert estimate <= equity_before * Decimal("0.01")
    assert observed_sells > 0
    assert table_dump(names) == before
    with db.read_connection() as conn:
        running = Decimal("100000.00")
        open_basis = {}
        for trade in conn.execute("SELECT * FROM stock_sim_trades ORDER BY id"):
            gross, fee = Decimal(trade["notional"]), Decimal(trade["fee"])
            assert fee == stock_sim._money(gross * stock_sim.FEE_RATE)
            assert gross == stock_sim._money(Decimal(trade["quantity"]) * Decimal(trade["price"]))
            if trade["side"] == "BUY":
                assert trade["symbol"] not in open_basis
                open_basis[trade["symbol"]] = (trade["quantity"], gross + fee)
                assert trade["realized_pnl"] is None
            else:
                quantity, basis = open_basis.pop(trade["symbol"])
                assert trade["quantity"] == quantity
                assert Decimal(trade["realized_pnl"]) == gross - fee - basis
            running += (gross - fee) if trade["side"] == "SELL" else -(gross + fee)
            assert running >= 0
        assert running == Decimal(conn.execute("SELECT cash FROM stock_sim_account").fetchone()[0])
        persisted = {r["symbol"]: (r["quantity"], Decimal(r["cost_basis"]))
                     for r in conn.execute("SELECT * FROM stock_sim_positions")}
        assert persisted == open_basis
        assert conn.execute("SELECT count(*) FROM stock_sim_equity_snapshots").fetchone()[0] == 41


def test_twenty_five_symbol_watchlist_and_removed_held_symbols(demo):
    set_config(stock_source_mode="simulation", stock_symbols=["IBM", "MSFT", "AAPL"])
    for _ in range(10):
        state = demo.post("/api/simulation/stocks/cycle").json()
        if len(state["positions"]) == 3:
            break
    assert state["positions"]
    held = {p["symbol"] for p in state["positions"]}
    watchlist = [chr(65 + n) for n in range(25)]
    set_config(stock_symbols=watchlist)
    state = demo.get("/api/simulation/stocks").json()
    assert len(state["candidates"]) == 25 + len(held)
    assert held <= {c["symbol"] for c in state["candidates"] if not c["in_watchlist"]}
    cycle = demo.post("/api/simulation/stocks/cycle").json()
    assert cycle["summary"]["scanned"] == 25 + len(held)
    assert len(cycle["positions"]) <= 3


def test_removed_watchlist_holding_is_marked_or_exits_with_reason(demo):
    set_config(stock_source_mode="simulation", stock_symbols=["IBM", "AAPL", "MSFT", "ORCL", "TSLA"])
    held = None
    for _ in range(10):
        state = demo.post("/api/simulation/stocks/cycle").json()
        if state["positions"]:
            held = state["positions"][0]["symbol"]
            break
    assert held
    set_config(stock_symbols=["ZZZ"] if held != "ZZZ" else ["AAA"])
    state = demo.post("/api/simulation/stocks/cycle").json()
    remaining = next((p for p in state["positions"] if p["symbol"] == held), None)
    if remaining:
        expected = stock_sim.make_candidates([held], state["step"], state["observed_at"])[0]
        assert remaining["mark_price"] == pytest.approx(float(Decimal(expected["price_usd"])))
        assert next(c for c in state["candidates"] if c["symbol"] == held)["in_watchlist"] is False
    else:
        assert any(t["symbol"] == held and t["side"] == "SELL" and t["step"] == state["step"] and "Synthetic exit" in t["reason"] for t in state["trades"])


def test_concurrent_first_get_and_cycles_are_serialized(demo):
    set_config(stock_source_mode="simulation", stock_symbols=["IBM", "AAPL", "MSFT", "ORCL", "TSLA"])
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        states = list(pool.map(lambda _: stock_sim.stock_simulation_state(), range(8)))
    assert all(s["step"] == 0 and s["initial_cash"] == 100000 for s in states)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        states = list(pool.map(lambda _: stock_sim.stock_simulation_cycle(), range(16)))
    assert sorted(s["step"] for s in states) == list(range(1, 17))
    assert all(s["cash"] >= 0 and len(s["positions"]) <= 3 and s["summary"]["buys"] <= 1 for s in states)
    with db.read_connection() as conn:
        assert conn.execute("SELECT count(*) FROM stock_sim_account").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM stock_sim_equity_snapshots").fetchone()[0] == 17
        assert conn.execute("SELECT count(*) FROM stock_sim_positions").fetchone()[0] <= 3


def test_regular_user_can_run_cycle_but_not_change_settings(demo, monkeypatch):
    set_config(stock_source_mode="simulation", stock_symbols=["IBM"])
    monkeypatch.setenv("APP_AUTH_ENABLED", "1")
    monkeypatch.setattr(auth, "get_session", lambda _: {"id": 3, "username": "user", "role": "user", "csrf": "x" * 43})
    guarded = FastAPI()
    guarded.include_router(stock_sim.router)
    @guarded.put("/api/settings")
    def settings():
        return {"unexpected": True}
    guarded.add_middleware(auth.AuthGuardMiddleware)
    with TestClient(guarded, cookies={auth.COOKIE: "anything"}) as client:
        assert client.get("/api/simulation/stocks").status_code == 200
        assert client.post("/api/simulation/stocks/cycle").status_code == 403
        headers = {"x-csrf-token": "x" * 43}
        assert client.post("/api/simulation/stocks/cycle", headers=headers).status_code == 200
        assert client.put("/api/settings", headers=headers).status_code == 403
