"""Paper-only accounting and a deliberately limited DexScreener scanner."""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN, localcontext
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from app import db

D = lambda value: Decimal(str(value))
SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9._-]{0,23}$")
ADDRESS_RE = re.compile(r"^[A-Za-z0-9:_-]{1,160}$")
_scan_lock = asyncio.Lock()


def token_key(chain: str, address: str) -> tuple[str, str]:
    # EVM addresses are case insensitive; Solana and other base58 addresses are not.
    return chain.lower(), address.lower() if address.startswith(("0x", "0X")) else address


class TradeError(Exception):
    pass


class ScanError(Exception):
    pass


def safe_number(value: Any, *, positive: bool = False) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = D(value)
        if not number.is_finite() or abs(number) > D("1e18") or number.as_tuple().exponent < -18 or (positive and number <= 0):
            return None
        return number
    except (InvalidOperation, ValueError, TypeError):
        return None


def _f(value: Any) -> float | None:
    return None if value is None else float(value)


def _url(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 500:
        return None
    parsed = urlparse(value)
    # Do not offer clickable arbitrary links supplied by a token profile.
    return value if parsed.scheme == "https" and parsed.hostname in {"dexscreener.com", "www.dexscreener.com"} else None


def portfolio(conn) -> dict[str, Any]:
    with localcontext() as ctx:
        ctx.prec = 50
        cash = D(conn.execute("SELECT cash FROM account WHERE id=1").fetchone()[0])
        positions = []
        for row in conn.execute("SELECT * FROM positions ORDER BY id DESC"):
            q, basis = D(row["quantity"]), D(row["cost_basis"])
            mark = D(row["mark_price"]) if row["mark_price"] is not None else None
            positions.append({"id": row["id"], "symbol": row["symbol"], "asset_type": row["asset_type"],
                "quantity": float(q), "entry_price": float(basis / q), "mark_price": _f(mark),
                "unrealized_pnl": _f(q * mark - basis) if mark is not None else None,
                "chain": row["chain"], "pair_address": row["pair_address"],
                "mark_source": row["mark_source"], "mark_timestamp": row["mark_timestamp"]})
        equity, unrealized = db.marked_equity(conn)
        sell_rows = conn.execute("SELECT realized_pnl FROM trades WHERE side='SELL'").fetchall()
        realized = sum((D(r[0]) for r in sell_rows), D(0))
        winners = sum(1 for r in sell_rows if D(r[0]) > 0)
        summary = {"cash": float(cash), "equity": _f(equity), "realized_pnl": float(realized),
                   "unrealized_pnl": _f(unrealized), "open_positions": len(positions),
                   "closed_trades": len(sell_rows), "win_rate": 100.0 * winners / len(sell_rows) if sell_rows else None}
        return {"positions": positions, "summary": summary}


def dashboard() -> dict[str, Any]:
    with db.read_connection() as conn:
        result = portfolio(conn)
        result["settings"] = db.get_settings(conn)
        result["agents"] = [dict(row) for row in conn.execute("SELECT id,name,status,detail FROM agents ORDER BY rowid")]
        result["candidates"] = []
        for row in conn.execute("SELECT symbol,chain,pair_address,price_usd,liquidity_usd,volume_24h,price_change_24h,volume_spike_ratio,pair_created_at,risk_status,reason,url,observed_at FROM candidates ORDER BY id"):
            item = dict(row)
            for k in ("price_usd", "liquidity_usd", "volume_24h", "price_change_24h", "volume_spike_ratio"):
                item[k] = _f(item[k])
            result["candidates"].append(item)
        result["trades"] = []
        for row in conn.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 250"):
            item = dict(row)
            for k in ("quantity", "reference_price", "fill_price", "slippage_bps", "fee", "realized_pnl"):
                item[k] = _f(item[k])
            result["trades"].append(item)
        result["equity_curve"] = [{"timestamp": r["timestamp"], "equity": _f(r["equity"])} for r in conn.execute("SELECT timestamp,equity FROM equity_snapshots ORDER BY id DESC LIMIT 1000").fetchall()[::-1]]
        result["activity"] = [dict(row) for row in conn.execute("SELECT timestamp,agent,message,level FROM activity ORDER BY id DESC LIMIT 250")]
        return result


def update_settings(patch: dict[str, Any]) -> dict[str, Any]:
    with db.transaction() as conn:
        settings = db.get_settings(conn)
        if "starting_balance" in patch and D(patch["starting_balance"]) != D(settings["starting_balance"]):
            if conn.execute("SELECT 1 FROM trades LIMIT 1").fetchone():
                raise TradeError("Starting balance cannot be changed after the first trade.")
            conn.execute("UPDATE account SET cash=? WHERE id=1", (str(patch["starting_balance"]),))
            conn.execute("DELETE FROM equity_snapshots")
        settings.update(patch)
        import json
        conn.execute("UPDATE settings SET data=? WHERE id=1", (json.dumps(settings, allow_nan=False),))
        if "starting_balance" in patch:
            db.snapshot(conn)
        db.log(conn, "settings", "Settings updated: " + ", ".join(sorted(patch)))
        if "auto_paper" in patch:
            db.set_agent(conn, "executor", "ready" if settings["auto_paper"] else "idle", "Auto-paper enabled for manual cycles only." if settings["auto_paper"] else "Auto-paper disabled; manual trades only. No live orders.")
        return settings


def _execute_in_transaction(conn, *, symbol: str, asset_type: str, side: str, quantity: Decimal,
                            price: Decimal, reason: str, chain: str | None = None,
                            pair_address: str | None = None, quote_time: str | None = None,
                            automatic: bool = False) -> dict[str, Any]:
    settings = db.get_settings(conn)
    if automatic and not settings["auto_paper"]:
        raise TradeError("Auto-paper has been disabled; no order was placed.")
    row = conn.execute("SELECT * FROM positions WHERE symbol=? AND asset_type=?", (symbol, asset_type)).fetchone()
    if automatic and row is not None:
        raise TradeError("Position already open for this symbol; automatic duplicate skipped.")
    slip, fee_rate = D(settings["slippage_bps"]) / D(10000), D(settings["fee_bps"]) / D(10000)
    fill = price * (D(1) + slip if side == "BUY" else D(1) - slip)
    notional = quantity * fill
    fee = notional * fee_rate
    cash = D(conn.execute("SELECT cash FROM account WHERE id=1").fetchone()[0])
    realized = None
    timestamp = db.utc_now()
    if side == "BUY":
        total = notional + fee
        if total > cash:
            raise TradeError("Insufficient paper cash including simulated fees and slippage.")
        cash -= total
        if row:
            conn.execute("UPDATE positions SET quantity=?,cost_basis=? WHERE id=?", (str(D(row["quantity"]) + quantity), str(D(row["cost_basis"]) + total), row["id"]))
        else:
            # A manual order's reference price is not a current mark. A scanner order
            # may use its independently timestamped live quote as a mark.
            conn.execute("INSERT INTO positions(symbol,asset_type,quantity,cost_basis,mark_price,mark_source,mark_timestamp,chain,pair_address,opened_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (symbol, asset_type, str(quantity), str(total), str(price) if quote_time else None,
                 "dexscreener" if quote_time else None, quote_time, chain, pair_address, timestamp))
    else:
        if not row or quantity > D(row["quantity"]):
            raise TradeError("Insufficient position quantity; short sales are not allowed.")
        held, basis = D(row["quantity"]), D(row["cost_basis"])
        allocated = basis if quantity == held else basis * quantity / held
        realized = notional - fee - allocated
        cash += notional - fee
        if quantity == held:
            conn.execute("DELETE FROM positions WHERE id=?", (row["id"],))
        else:
            conn.execute("UPDATE positions SET quantity=?,cost_basis=? WHERE id=?", (str(held - quantity), str(basis - allocated), row["id"]))
        chain, pair_address = row["chain"], row["pair_address"]
    conn.execute("UPDATE account SET cash=? WHERE id=1", (str(cash),))
    cur = conn.execute("INSERT INTO trades(timestamp,symbol,side,asset_type,quantity,reference_price,fill_price,slippage_bps,fee,realized_pnl,reason,chain,pair_address) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (timestamp, symbol, side, asset_type, str(quantity), str(price), str(fill), str(settings["slippage_bps"]), str(fee), str(realized) if realized is not None else None, reason, chain, pair_address))
    db.log(conn, "executor", f"Paper {side} {quantity} {symbol} at {fill}; reason: {reason}")
    db.snapshot(conn)
    return {"id": cur.lastrowid, "timestamp": timestamp, "symbol": symbol, "side": side,
        "asset_type": asset_type, "quantity": float(quantity), "reference_price": float(price),
        "fill_price": float(fill), "slippage_bps": float(settings["slippage_bps"]), "fee": float(fee),
        "realized_pnl": _f(realized), "reason": reason, "chain": chain, "pair_address": pair_address}


def manual_trade(symbol: str, asset_type: str, side: str, quantity: Any, price: Any, reason: str) -> dict[str, Any]:
    with localcontext() as ctx:
        ctx.prec = 50
        with db.transaction() as conn:
            # Detect repeated form submissions under the same SQLite write lock as
            # the trade. Intentional identical orders can be sent after two seconds.
            clean_reason = db.clean_text(reason, 300)
            cutoff = (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat(timespec="microseconds").replace("+00:00", "Z")
            recent = conn.execute("SELECT quantity,reference_price FROM trades WHERE symbol=? AND asset_type=? AND side=? AND reason=? AND timestamp>=?", (symbol, asset_type, side, clean_reason, cutoff))
            if any(D(row["quantity"]) == D(quantity) and D(row["reference_price"]) == D(price) for row in recent):
                raise TradeError("An identical paper order was just recorded; wait two seconds before intentionally repeating it.")
            return _execute_in_transaction(conn, symbol=symbol, asset_type=asset_type, side=side,
                quantity=D(quantity), price=D(price), reason=clean_reason)


def mark_position(position_id: int, price: Any) -> dict[str, Any]:
    with localcontext() as ctx:
        ctx.prec = 50
        with db.transaction() as conn:
            row = conn.execute("SELECT * FROM positions WHERE id=?", (position_id,)).fetchone()
            if row is None:
                raise TradeError("Position not found.")
            now = db.utc_now()
            conn.execute("UPDATE positions SET mark_price=?,mark_source='manual',mark_timestamp=? WHERE id=?", (str(price), now, position_id))
            db.log(conn, "equity_curve", f"Manual mark for {row['symbol']}: {price}.")
            db.snapshot(conn)
            return {"id": position_id, "symbol": row["symbol"], "price": float(price), "mark_price": float(price), "mark_source": "manual", "mark_timestamp": now}


def _candidate(pair: dict[str, Any], settings: dict[str, Any], observed: str) -> dict[str, Any] | None:
    base = pair.get("baseToken") or {}
    if not isinstance(base, dict):
        return None
    symbol = str(base.get("symbol") or "").upper()
    chain = str(pair.get("chainId") or "")
    address = str(pair.get("pairAddress") or "")
    if not SYMBOL_RE.fullmatch(symbol) or not ADDRESS_RE.fullmatch(chain) or not ADDRESS_RE.fullmatch(address):
        return None
    liquidity = safe_number((pair.get("liquidity") or {}).get("usd")) if isinstance(pair.get("liquidity"), dict) else None
    volume = safe_number((pair.get("volume") or {}).get("h24")) if isinstance(pair.get("volume"), dict) else None
    volume_h1 = safe_number((pair.get("volume") or {}).get("h1")) if isinstance(pair.get("volume"), dict) else None
    spike = volume_h1 * D(23) / (volume - volume_h1) if volume is not None and volume_h1 is not None and 0 <= volume_h1 < volume else None
    pair_created_at = None
    created = safe_number(pair.get("pairCreatedAt"), positive=True)
    if created is not None:
        try:
            pair_created_at = datetime.fromtimestamp(float(created / 1000), timezone.utc).isoformat().replace("+00:00", "Z")
        except (OverflowError, OSError, ValueError):
            pass
    price = safe_number(pair.get("priceUsd"), positive=True)
    change = safe_number((pair.get("priceChange") or {}).get("h24")) if isinstance(pair.get("priceChange"), dict) else None
    reasons = []
    if price is None: reasons.append("missing or invalid USD price")
    if liquidity is None or liquidity < 0: reasons.append("missing or invalid liquidity")
    elif liquidity < D(settings["min_liquidity"]): reasons.append("liquidity below minimum")
    if volume is None or volume < 0: reasons.append("missing or invalid 24h volume")
    elif volume < D(settings["min_volume"]): reasons.append("24h volume below minimum")
    return {"symbol": symbol, "chain": chain, "pair_address": address,
        "price_usd": str(price) if price is not None else None,
        "liquidity_usd": str(liquidity) if liquidity is not None and liquidity >= 0 else None,
        "volume_24h": str(volume) if volume is not None and volume >= 0 else None,
        "price_change_24h": str(change) if change is not None else None,
        "volume_spike_ratio": str(spike) if spike is not None else None,
        "pair_created_at": pair_created_at,
        "risk_status": "filtered" if reasons else "eligible",
        "reason": "; ".join(reasons) if reasons else "Meets liquidity and volume thresholds; not a safety or rug-pull assessment.",
        "url": _url(pair.get("url")), "observed_at": observed}


async def scan_market() -> dict[str, Any]:
    async with _scan_lock:
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=False, headers={"Accept": "application/json", "User-Agent": "LocalPaperLedger/1.0"}) as client:
                response = await client.get("https://api.dexscreener.com/token-profiles/latest/v1")
                response.raise_for_status()
                profiles = response.json()
                if not isinstance(profiles, list):
                    raise ScanError("Token profiles response is not a list.")
                # Limit a public endpoint scan to the first 30 unique token profiles.
                tokens = []
                for profile in profiles:
                    if not isinstance(profile, dict):
                        continue
                    chain, addr = str(profile.get("chainId") or ""), str(profile.get("tokenAddress") or "")
                    if ADDRESS_RE.fullmatch(chain) and ADDRESS_RE.fullmatch(addr) and token_key(chain, addr) not in [token_key(c, a) for c, a in tokens]:
                        tokens.append((chain, addr))
                    if len(tokens) >= 30:
                        break
                pairs = []
                if tokens:
                    response = await client.get("https://api.dexscreener.com/latest/dex/tokens/" + quote(",".join(a for _, a in tokens), safe=","))
                    response.raise_for_status()
                    payload = response.json()
                    if not isinstance(payload, dict) or (payload.get("pairs") is not None and not isinstance(payload["pairs"], list)):
                        raise ScanError("Token pairs response has an unexpected format.")
                    pairs = payload.get("pairs") or []
            observed = db.utc_now()
            with db.transaction() as conn:
                settings = db.get_settings(conn)
                # Only a pair's base token price is its priceUsd. Do not mistake the
                # price of a quote token for the price of the profiled token.
                matches: dict[tuple[str, str], list[dict[str, Any]]] = {}
                selected = {token_key(c, a) for c, a in tokens}
                for pair in pairs:
                    if not isinstance(pair, dict) or not isinstance(pair.get("baseToken"), dict):
                        continue
                    key = token_key(str(pair.get("chainId") or ""), str(pair["baseToken"].get("address") or ""))
                    if key not in selected:
                        continue
                    item = _candidate(pair, settings, observed)
                    if item:
                        matches.setdefault(key, []).append(item)
                candidates = []
                for chain, token in tokens:
                    options = matches.get(token_key(chain, token), [])
                    if options:
                        # Use the most liquid pool for a token, not every duplicate pair.
                        candidates.append(max(options, key=lambda x: D(x["liquidity_usd"]) if x["liquidity_usd"] is not None else D(-1)))
                conn.execute("DELETE FROM candidates")
                for item in candidates:
                    conn.execute("INSERT INTO candidates(symbol,chain,pair_address,price_usd,liquidity_usd,volume_24h,price_change_24h,volume_spike_ratio,pair_created_at,risk_status,reason,url,observed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", tuple(item[k] for k in ("symbol", "chain", "pair_address", "price_usd", "liquidity_usd", "volume_24h", "price_change_24h", "volume_spike_ratio", "pair_created_at", "risk_status", "reason", "url", "observed_at")))
                    # Only refresh a position if it has this exact on-chain pair identity.
                    if item["price_usd"] is not None:
                        conn.execute("UPDATE positions SET mark_price=?,mark_source='dexscreener',mark_timestamp=? WHERE asset_type='crypto' AND chain=? AND pair_address=?", (item["price_usd"], observed, item["chain"], item["pair_address"]))
                db.snapshot(conn)
                eligible = sum(c["risk_status"] == "eligible" for c in candidates)
                detail = f"Scanned {len(tokens)} latest token profiles; {len(candidates)} matched base-token pairs, {eligible} meet basic thresholds."
                db.set_agent(conn, "scanner", "ready", detail)
                db.set_agent(conn, "rug_check", "ready", "Liquidity/volume checks only; cannot identify scams, honeypots or rug pulls.")
                db.log(conn, "scanner", detail)
            return {"observed_at": observed, "profiles_scanned": len(tokens), "candidates": [{**c, **{k: _f(c[k]) for k in ("price_usd", "liquidity_usd", "volume_24h", "price_change_24h", "volume_spike_ratio")}} for c in candidates]}
        except (httpx.HTTPError, ValueError, ScanError) as exc:
            # Do not leak raw upstream URLs, response bodies, or tokens into logs.
            detail = "DexScreener scan failed; previously saved candidates may be stale."
            with db.transaction() as conn:
                db.set_agent(conn, "scanner", "error", detail)
                db.log(conn, "scanner", detail, "error")
            raise ScanError(detail) from exc


def auto_buy(candidate: dict[str, Any]) -> dict[str, Any]:
    with localcontext() as ctx:
        ctx.prec = 50
        with db.transaction() as conn:
            settings = db.get_settings(conn)
            if not settings["auto_paper"]:
                raise TradeError("Auto-paper is disabled.")
            if candidate["risk_status"] != "eligible" or D(candidate["liquidity_usd"]) < D(settings["min_liquidity"]) or D(candidate["volume_24h"]) < D(settings["min_volume"]):
                raise TradeError("Candidate does not meet current thresholds.")
            equity, _ = db.marked_equity(conn)
            if equity is None:
                raise TradeError("Cannot size with unmarked positions; mark every open position first.")
            cash = D(conn.execute("SELECT cash FROM account WHERE id=1").fetchone()[0])
            price = D(candidate["price_usd"])
            slip, fee = D(settings["slippage_bps"]) / D(10000), D(settings["fee_bps"]) / D(10000)
            cost_per_unit = price * (D(1) + slip) * (D(1) + fee)
            # Planning estimate, not an actual stop order. Stops may gap or fail.
            stop_sale_per_unit = price * (D(1) - D(settings["stop_loss_percent"]) / D(100)) * (D(1) - slip) * (D(1) - fee)
            risk_per_unit = cost_per_unit - stop_sale_per_unit
            budget = equity * D(settings["risk_percent"]) / D(100)
            cap = equity * D(settings["max_position_percent"]) / D(100)
            quantity = min(budget / risk_per_unit, cap / cost_per_unit, cash / cost_per_unit, D("1e15"))
            quantity = quantity.quantize(D("0.000000000000000001"), rounding=ROUND_DOWN)
            if quantity <= 0:
                raise TradeError("Not enough equity or cash to place a sized paper buy.")
            reason = "Automatic paper cycle: liquidity/volume thresholds met; risk and position sizing applied."
            trade = _execute_in_transaction(conn, symbol=candidate["symbol"], asset_type="crypto", side="BUY", quantity=quantity, price=price, reason=reason, chain=candidate["chain"], pair_address=candidate["pair_address"], quote_time=candidate["observed_at"], automatic=True)
            db.set_agent(conn, "sizer", "ready", "Risk-sized paper buys use marked equity and simulated costs; no automatic stop order is placed.")
            return trade
