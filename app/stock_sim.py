"""Explicit-click, offline stock demonstration, isolated from the paper ledger.

The generated numbers are invented and are not quotes, market data, advice, or
orders.  This module never contacts a network or uses a broker, API key or LLM.
The host application initializes its ordinary database and mounts ``router``;
its authentication/CSRF middleware controls access to these routes.
"""
from __future__ import annotations

import hashlib
import re
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP, localcontext
from typing import Any

from fastapi import APIRouter, HTTPException

from app import db

D = Decimal
INITIAL_CASH = D("100000.00")
FEE_RATE = D("0.001")
STOP_FRACTION = D("0.10")
RISK_FRACTION = D("0.01")
CAP_FRACTION = D("0.10")
MAX_POSITIONS = 3
MAX_STEP = 1_000_000_000
MAX_EQUITY = D("1000000000000000")
MAX_SYMBOLS = 28  # Up to 25 watched symbols plus three previously-held symbols.
SYMBOL_RE = re.compile(r"[A-Z]{1,5}(?:[.-][A-Z]{1,4})?\Z", re.ASCII)
DISCLOSURE = "Synthetic demonstration data, not a market quote; not for real trading"
NOTICE = (DISCLOSURE + ". All prices, signals, fills and results are invented; "
          "no live orders, provider requests, background worker or brokerage connection. "
          "The demo account is separate from your paper ledger.")
RULES = ("Each click advances one synthetic step. Screen: positive change from the prior "
         "synthetic step. At most one new whole-share buy per click and three open positions. "
         "Size using a 1% equity loss estimate at a hypothetical 10% price decline, with "
         "a 10% equity entry-cost cap and 0.1% simulated fees. Exit at a 3% decline, "
         "4% gain, or after eight steps; actual simulated fills can cross thresholds. "
         "These are rules for invented data, not actual stop orders or guarantees.")

# DDL is executed statement by statement inside BEGIN IMMEDIATE: executescript
# would implicitly commit, breaking atomic first-use initialization.
_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS stock_sim_account (
        id INTEGER PRIMARY KEY CHECK(id=1), initial_cash TEXT NOT NULL,
        cash TEXT NOT NULL, step INTEGER NOT NULL CHECK(step>=0), updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS stock_sim_positions (
        symbol TEXT PRIMARY KEY, quantity INTEGER NOT NULL CHECK(quantity>0),
        cost_basis TEXT NOT NULL, entry_reference TEXT NOT NULL, mark_price TEXT NOT NULL,
        opened_step INTEGER NOT NULL, opened_at TEXT NOT NULL, mark_timestamp TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS stock_sim_trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, step INTEGER NOT NULL,
        symbol TEXT NOT NULL, side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
        quantity INTEGER NOT NULL CHECK(quantity>0), price TEXT NOT NULL, notional TEXT NOT NULL,
        fee TEXT NOT NULL, realized_pnl TEXT, reason TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS stock_sim_activity (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, step INTEGER NOT NULL,
        agent TEXT NOT NULL, message TEXT NOT NULL, level TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS stock_sim_equity_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, step INTEGER NOT NULL,
        cash TEXT NOT NULL, equity TEXT NOT NULL)""",
)


def _symbols(values: Any, *, maximum: int = MAX_SYMBOLS) -> list[str]:
    if not isinstance(values, (list, tuple)) or len(values) > maximum:
        raise ValueError("Invalid simulation watchlist size.")
    result = []
    for value in values:
        if not isinstance(value, str) or not value.isascii():
            raise ValueError("Invalid simulation symbol.")
        symbol = value.strip().upper()
        if not SYMBOL_RE.fullmatch(symbol) or symbol in result:
            raise ValueError("Invalid or duplicate simulation symbol.")
        result.append(symbol)
    return result


def _price(symbol: str, step: int) -> Decimal:
    """An arbitrary repeating wave; the symbol hash does not encode real prices.

    The sub-cent suffix is an injective base-29 representation of the ticker,
    so distinct supported symbols never accidentally acquire identical prices.
    It is less than one cent and independent of the other symbols in a scan.
    """
    digest = hashlib.sha256(("paperledger-offline-v1:" + symbol).encode("ascii")).digest()
    seed = int.from_bytes(digest[:4], "big")
    base_cents = 1500 + seed % 25001
    period = 16 + digest[4] % 9
    phase = digest[5] % period
    point = (step + phase) % period
    height = min(point, period - point)
    bps = -1500 + 3000 * height // (period // 2)
    cents = max(1, base_cents * (10000 + bps) // 10000)
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ.-"
    identity = 0
    for char in symbol:
        identity = identity * 29 + alphabet.index(char) + 1
    return D(cents) / D(100) + D(identity) / D(10 ** 18)


def make_candidates(symbols: list[str], step: int, observed_at: str) -> list[dict[str, Any]]:
    """Return SQLite-compatible synthetic candidate dicts for the given step.

    Decimal values are encoded as exact decimal strings, matching the existing
    scanner's candidate contract; callers can use Decimal(value) without a
    float round trip. ``observed_at`` is the real generation time supplied by
    the caller, not a quote timestamp. No network or database is used here.
    """
    symbols = _symbols(symbols)
    if isinstance(step, bool) or not isinstance(step, int) or not 0 <= step <= MAX_STEP:
        raise ValueError("Invalid simulation step.")
    if not isinstance(observed_at, str) or db.parse_timestamp(observed_at) is None:
        raise ValueError("Simulation observation time must be timezone-aware.")
    candidates = []
    with localcontext() as ctx:
        ctx.prec = 50
        for symbol in symbols:
            price, previous = _price(symbol, step), _price(symbol, step - 1)
            momentum = ((price - previous) * D(100) / previous).quantize(D("0.0001"), rounding=ROUND_HALF_UP)
            candidates.append({
                "symbol": symbol, "chain": None, "pair_address": None,
                "price_usd": str(price), "liquidity_usd": None, "volume_24h": None,
                "price_change_24h": None, "volume_spike_ratio": None, "pair_created_at": None,
                "risk_status": "simulated", "reason": DISCLOSURE + ". Price and step momentum are invented.",
                "url": None, "observed_at": observed_at, "asset_type": "stock",
                "source": "Simulation", "quote_date": None, "quote_timestamp": None,
                "stock_volume_shares": None, "stock_change_percent": None,
                "cache_status": "simulated", "demo_momentum_percent": str(momentum),
                "demo_previous_price_usd": str(previous),
            })
    return candidates


def current_step() -> int:
    """Read the persisted demo step, or 0 before the demo has been initialized."""
    with db.read_connection() as conn:
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='stock_sim_account'").fetchone()
        if not exists:
            return 0
        row = conn.execute("SELECT step FROM stock_sim_account WHERE id=1").fetchone()
        return int(row[0]) if row else 0


def _money(value: Decimal) -> Decimal:
    return value.quantize(D("0.01"), rounding=ROUND_HALF_UP)


def _activity(conn, timestamp: str, step: int, agent: str, message: str, level: str = "info") -> None:
    conn.execute("INSERT INTO stock_sim_activity(timestamp,step,agent,message,level) VALUES(?,?,?,?,?)",
                 (timestamp, step, agent, message, level))


def _initialize(conn, now: str) -> None:
    for statement in _SCHEMA:
        conn.execute(statement)
    inserted = conn.execute("INSERT OR IGNORE INTO stock_sim_account(id,initial_cash,cash,step,updated_at) VALUES(1,?,?,0,?)",
                            (str(INITIAL_CASH), str(INITIAL_CASH), now)).rowcount
    if inserted:
        conn.execute("INSERT INTO stock_sim_equity_snapshots(timestamp,step,cash,equity) VALUES(?,0,?,?)",
                     (now, str(INITIAL_CASH), str(INITIAL_CASH)))
        _activity(conn, now, 0, "simulation", "Offline synthetic demo initialized with separate $100,000 demo cash; no ledger funds changed.")


def _has_account(conn) -> bool:
    table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='stock_sim_account'").fetchone()
    return bool(table and conn.execute("SELECT 1 FROM stock_sim_account WHERE id=1").fetchone())


def _watchlist(settings: dict[str, Any]) -> list[str]:
    try:
        return _symbols(settings.get("stock_symbols", []), maximum=25)
    except ValueError:
        raise HTTPException(409, "The stock watchlist is invalid for the offline simulation.") from None


def _valuation(conn) -> tuple[Decimal, Decimal, list]:
    row = conn.execute("SELECT cash FROM stock_sim_account WHERE id=1").fetchone()
    cash = D(row["cash"])
    positions = conn.execute("SELECT * FROM stock_sim_positions ORDER BY symbol").fetchall()
    equity = cash + sum((_money(D(p["quantity"]) * D(p["mark_price"])) for p in positions), D(0))
    if not cash.is_finite() or cash < 0 or not equity.is_finite() or equity < 0 or equity > MAX_EQUITY:
        raise HTTPException(409, "Simulation balances are outside supported limits; no changes made.")
    return cash, equity, positions


def _order_cost(quantity: int, price: Decimal) -> tuple[Decimal, Decimal]:
    notional = _money(D(quantity) * price)
    return notional, _money(notional * FEE_RATE)


def _size(price: Decimal, cash: Decimal, equity: Decimal) -> int:
    # Hypothetical stop-based loss includes both entry and exit fees; the 10%
    # entry-cost cap and cash budget include the buy fee as well. This is an
    # estimate, not a guarantee of a stop or maximum realized loss.
    unit_cost = price * (D(1) + FEE_RATE)
    unit_loss = unit_cost - price * (D(1) - STOP_FRACTION) * (D(1) - FEE_RATE)
    amount = min(cash / unit_cost, equity * CAP_FRACTION / unit_cost,
                 equity * RISK_FRACTION / unit_loss, D("1000000000000000"))
    quantity = int(amount.to_integral_value(rounding=ROUND_DOWN))
    while quantity > 0:
        notional, fee = _order_cost(quantity, price)
        stop_notional = _money(D(quantity) * price * (D(1) - STOP_FRACTION))
        stop_fee = _money(stop_notional * FEE_RATE)
        cost = notional + fee
        if cost <= cash and cost <= equity * CAP_FRACTION and cost - stop_notional + stop_fee <= equity * RISK_FRACTION:
            return quantity
        quantity -= 1
    return 0


def _position_json(p) -> dict[str, Any]:
    quantity, basis, price = D(p["quantity"]), D(p["cost_basis"]), D(p["mark_price"])
    return {"symbol": p["symbol"], "quantity": int(quantity), "entry_price": float(basis / quantity),
            "mark_price": float(price), "unrealized_pnl": float(_money(quantity * price) - basis),
            "cost_basis": float(basis), "entry_reference": float(D(p["entry_reference"])),
            "opened_step": p["opened_step"], "opened_at": p["opened_at"],
            "mark_timestamp": p["mark_timestamp"], "mark_source": "Simulation"}


def _state(conn, *, enabled: bool, watchlist: list[str], now: str) -> dict[str, Any]:
    if not _has_account(conn):
        return {"enabled": False, "initial_cash": float(INITIAL_CASH), "cash": None,
                "equity": None, "step": 0, "positions": [], "trades": [], "activity": [],
                "equity_curve": [], "candidates": [], "notice": NOTICE, "rules": RULES,
                "message": "Demo is disabled and has not been initialized. An administrator can select simulation mode."}
    account = conn.execute("SELECT * FROM stock_sim_account WHERE id=1").fetchone()
    cash, equity, positions = _valuation(conn)
    trades = []
    for r in conn.execute("SELECT * FROM stock_sim_trades ORDER BY id DESC LIMIT 250"):
        trades.append({"timestamp": r["timestamp"], "step": r["step"], "symbol": r["symbol"],
                       "side": r["side"], "quantity": r["quantity"], "price": float(D(r["price"])),
                       "fee": float(D(r["fee"])), "notional": float(D(r["notional"])),
                       "realized_pnl": float(D(r["realized_pnl"])) if r["realized_pnl"] is not None else None,
                       "reason": r["reason"]})
    activity = [dict(r) for r in conn.execute("SELECT timestamp,step,agent,message,level FROM stock_sim_activity ORDER BY id DESC LIMIT 250")]
    history = conn.execute("SELECT timestamp,step,cash,equity FROM stock_sim_equity_snapshots ORDER BY id DESC LIMIT 250").fetchall()
    curve = [{"timestamp": r["timestamp"], "step": r["step"], "cash": float(D(r["cash"])),
              "equity": float(D(r["equity"]))} for r in reversed(history)]
    candidates = []
    if enabled:
        all_symbols = list(watchlist)
        for p in positions:
            if p["symbol"] not in all_symbols:
                all_symbols.append(p["symbol"])
        candidates = make_candidates(all_symbols, account["step"], now)
        for c in candidates:
            c["in_watchlist"] = c["symbol"] in watchlist
            for key in ("price_usd", "demo_momentum_percent", "demo_previous_price_usd"):
                c[key] = float(D(c[key]))
    return {"enabled": enabled, "initial_cash": float(D(account["initial_cash"])),
            "cash": float(cash), "equity": float(equity), "step": account["step"],
            "positions": [_position_json(p) for p in positions], "trades": trades,
            "activity": activity, "equity_curve": curve, "candidates": candidates,
            "observed_at": now if enabled else None, "last_updated_at": account["updated_at"],
            "notice": NOTICE, "rules": RULES,
            "message": "Offline simulation enabled." if enabled else "Demo is disabled; saved demo data is unchanged."}


router = APIRouter()


@router.get("/api/simulation/stocks")
def stock_simulation_state() -> dict[str, Any]:
    # Check mode in the same lock as first-use initialization. A disabled GET
    # reads saved demo state if present, but never creates it or advances time.
    with localcontext() as ctx:
        ctx.prec = 50
        with db.transaction() as conn:
            settings = db.get_settings(conn)
            enabled = settings.get("stock_source_mode") == "simulation"
            watchlist = _watchlist(settings) if enabled else []
            now = db.utc_now()
            if enabled:
                _initialize(conn, now)
            return _state(conn, enabled=enabled, watchlist=watchlist, now=now)


@router.post("/api/simulation/stocks/cycle")
def stock_simulation_cycle() -> dict[str, Any]:
    # Provider/mode selection, scan, marks, exits, sizing, buy and snapshots
    # are serialized by one SQLite write transaction across processes.
    with localcontext() as ctx:
        ctx.prec = 50
        with db.transaction() as conn:
            settings = db.get_settings(conn)
            if settings.get("stock_source_mode") != "simulation":
                raise HTTPException(409, "Select stock simulation mode before running a demo cycle.")
            watchlist = _watchlist(settings)
            now = db.utc_now()
            _initialize(conn, now)
            account = conn.execute("SELECT * FROM stock_sim_account WHERE id=1").fetchone()
            if account["step"] >= MAX_STEP:
                raise HTTPException(409, "The simulation has reached its supported step limit.")
            step = account["step"] + 1
            cash, _, held = _valuation(conn)
            all_symbols = list(watchlist)
            for p in held:
                if p["symbol"] not in all_symbols:
                    all_symbols.append(p["symbol"])
            candidates = make_candidates(all_symbols, step, now)
            by_symbol = {c["symbol"]: c for c in candidates}
            _activity(conn, now, step, "scan", f"Generated {len(candidates)} synthetic ticker path(s), including {len(all_symbols) - len(watchlist)} held outside the watchlist.")

            # Revalue every held ticker, even when it is no longer watched.
            for p in held:
                conn.execute("UPDATE stock_sim_positions SET mark_price=?,mark_timestamp=? WHERE symbol=?",
                             (by_symbol[p["symbol"]]["price_usd"], now, p["symbol"]))
            sold = []
            for p in held:
                price = D(by_symbol[p["symbol"]]["price_usd"])
                entry = D(p["entry_reference"])
                age = step - p["opened_step"]
                trigger = ("3% synthetic decline" if price <= entry * D("0.97") else
                           "4% synthetic gain" if price >= entry * D("1.04") else
                           "eight-step holding limit" if age >= 8 else None)
                if not trigger:
                    continue
                quantity = int(p["quantity"])
                notional, fee = _order_cost(quantity, price)
                proceeds = notional - fee
                realized = proceeds - D(p["cost_basis"])
                reason = f"Synthetic exit: {trigger}; no real order or actual stop."
                cash += proceeds
                conn.execute("DELETE FROM stock_sim_positions WHERE symbol=?", (p["symbol"],))
                conn.execute("INSERT INTO stock_sim_trades(timestamp,step,symbol,side,quantity,price,notional,fee,realized_pnl,reason) VALUES(?,?,?,'SELL',?,?,?,?,?,?)",
                             (now, step, p["symbol"], quantity, str(price), str(notional), str(fee), str(realized), reason))
                _activity(conn, now, step, "executor", f"Synthetic SELL {quantity} {p['symbol']}: {trigger}; fee ${fee}.")
                sold.append(p["symbol"])
            conn.execute("UPDATE stock_sim_account SET cash=? WHERE id=1", (str(cash),))
            cash, equity, remaining = _valuation(conn)
            held_symbols = {p["symbol"] for p in remaining}
            # Only watched tickers may be opened; previously-held non-watchlist
            # tickers remain marked/evaluated for exit, never silently deleted.
            eligible = [c for c in candidates if c["symbol"] in watchlist
                        and D(c["demo_momentum_percent"]) > 0
                        and c["symbol"] not in held_symbols and c["symbol"] not in sold]
            eligible.sort(key=lambda c: (-D(c["demo_momentum_percent"]), c["symbol"]))
            _activity(conn, now, step, "screen", f"Positive synthetic step momentum: {len(eligible)} unheld watched candidate(s); sold tickers are not re-entered this step.")
            buys = []
            if len(remaining) >= MAX_POSITIONS:
                size_message = "Three-position limit reached; no new buy."
            elif not eligible:
                size_message = "No unheld watched ticker passed the positive synthetic momentum screen."
            else:
                c = eligible[0]
                price = D(c["price_usd"])
                quantity = _size(price, cash, equity)
                if quantity == 0:
                    size_message = "No whole share fits the cash, 1% estimated-risk and 10% equity limits."
                else:
                    notional, fee = _order_cost(quantity, price)
                    cost = notional + fee
                    cash -= cost
                    reason = (f"Synthetic momentum +{c['demo_momentum_percent']}% versus the previous step; "
                              "sized at 1% estimated risk and 10% equity cap; no real order.")
                    conn.execute("INSERT INTO stock_sim_positions(symbol,quantity,cost_basis,entry_reference,mark_price,opened_step,opened_at,mark_timestamp) VALUES(?,?,?,?,?,?,?,?)",
                                 (c["symbol"], quantity, str(cost), str(price), str(price), step, now, now))
                    conn.execute("INSERT INTO stock_sim_trades(timestamp,step,symbol,side,quantity,price,notional,fee,realized_pnl,reason) VALUES(?,?,?,'BUY',?,?,?,?,NULL,?)",
                                 (now, step, c["symbol"], quantity, str(price), str(notional), str(fee), reason))
                    conn.execute("UPDATE stock_sim_account SET cash=? WHERE id=1", (str(cash),))
                    _activity(conn, now, step, "executor", f"Synthetic BUY {quantity} {c['symbol']}: positive step momentum; fee ${fee}.")
                    buys.append(c["symbol"])
                    size_message = f"Sized {quantity} whole share(s) of {c['symbol']} within demo risk, cap and cash limits."
            _activity(conn, now, step, "sizer", size_message)
            cash, equity, _ = _valuation(conn)
            conn.execute("UPDATE stock_sim_account SET step=?,updated_at=? WHERE id=1", (step, now))
            conn.execute("INSERT INTO stock_sim_equity_snapshots(timestamp,step,cash,equity) VALUES(?,?,?,?)", (now, step, str(cash), str(equity)))
            _activity(conn, now, step, "equity", f"Synthetic marks and separate demo equity updated; {len(buys)} buy(s), {len(sold)} sell(s).")
            result = _state(conn, enabled=True, watchlist=watchlist, now=now)
            result["summary"] = {"step": step, "scanned": len(candidates), "eligible": len(eligible),
                                 "buys": len(buys), "sells": len(sold), "bought_symbols": buys,
                                 "sold_symbols": sold, "message": size_message}
            return result
