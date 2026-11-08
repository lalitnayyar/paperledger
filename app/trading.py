"""Paper-only accounting and crypto/stock quote scanning; no live execution."""
from __future__ import annotations

import asyncio
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN, localcontext
from typing import Any
from urllib.parse import quote, urlparse

import httpx
from fastapi import HTTPException

from app import db, market_data

D = lambda value: Decimal(str(value))
SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9._-]{0,23}$")
STOCK_SYMBOL_RE = re.compile(r"^[A-Z]{1,5}(?:[.-][A-Z]{1,4})?$")
ADDRESS_RE = re.compile(r"^[A-Za-z0-9:_-]{1,160}$")
_scan_lock = asyncio.Lock()
STOCK_CACHE_TTL = timedelta(hours=6)


def _stock_cache_is_fresh(observed_at: Any, now: datetime | None = None) -> bool:
    observed = db.parse_timestamp(observed_at)
    now = now or datetime.now(timezone.utc)
    return observed is not None and timedelta(0) <= now - observed < STOCK_CACHE_TTL


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
        mode = result["settings"]["market_mode"]
        for row in conn.execute("SELECT symbol,chain,pair_address,price_usd,liquidity_usd,volume_24h,price_change_24h,volume_spike_ratio,pair_created_at,risk_status,reason,url,observed_at,asset_type,source,quote_date,stock_volume_shares,stock_change_percent,cache_status FROM candidates WHERE (?='both' OR asset_type=?) ORDER BY id", (mode, "stock" if mode == "stocks" else "crypto")):
            item = dict(row)
            if item["asset_type"] == "stock":
                if item["symbol"] not in result["settings"]["stock_symbols"]:
                    continue
                item["chain"] = item["pair_address"] = None
                item["cache_status"] = (item["cache_status"] if item["cache_status"] in ("fresh", "cached") else "cached") if _stock_cache_is_fresh(item["observed_at"]) else "stale"
            else:
                item["cache_status"] = None
            for k in ("price_usd", "liquidity_usd", "volume_24h", "price_change_24h", "volume_spike_ratio", "stock_change_percent"):
                item[k] = _f(item[k])
            result["candidates"].append(item)
        if mode in ("stocks", "both"):
            # Manual imports are separate from scanned candidates and provider cache;
            # never feed them to the scanner, automatic orders, or position marks.
            for row in conn.execute("SELECT symbol,price_usd,quote_date,stock_volume_shares,observed_at FROM manual_stock_quotes ORDER BY rowid"):
                result["candidates"].append({
                    "symbol": row["symbol"], "chain": None, "pair_address": None,
                    "price_usd": _f(row["price_usd"]), "liquidity_usd": None, "volume_24h": None,
                    "price_change_24h": None, "volume_spike_ratio": None, "pair_created_at": None,
                    "risk_status": "manual only",
                    "reason": "User-provided Yahoo Finance import, not independently verified; US listing and USD are assumed. Quote date is optional and user-supplied; the import time is not the quote time. Price and volume may be stale or incomplete. Manual paper trading only; no automatic buy, position mark, liquidity check, or risk assessment.",
                    "url": None, "observed_at": row["observed_at"], "asset_type": "stock",
                    "source": "User import (Yahoo Finance)", "quote_date": row["quote_date"],
                    "stock_volume_shares": row["stock_volume_shares"], "stock_change_percent": None,
                    "cache_status": "manual",
                })
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
        if "stock_symbols" in patch:
            # Removal is immediate and irreversible from the local cache: adding the
            # ticker again requires another provider quote, not a revived old price.
            placeholders = ",".join("?" for _ in settings["stock_symbols"])
            conn.execute(f"DELETE FROM stock_quote_cache WHERE symbol NOT IN ({placeholders})", settings["stock_symbols"])
            conn.execute(f"DELETE FROM candidates WHERE asset_type='stock' AND symbol NOT IN ({placeholders})", settings["stock_symbols"])
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
    if automatic and (asset_type != "crypto" or settings["market_mode"] not in ("crypto", "both")):
        raise TradeError("Automatic paper orders are restricted to crypto in the current market mode.")
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
        "url": _url(pair.get("url")), "observed_at": observed,
        "asset_type": "crypto", "source": "DexScreener", "quote_date": None,
        "stock_volume_shares": None, "stock_change_percent": None, "cache_status": None}


async def _scan_crypto(settings: dict[str, Any]) -> tuple[list[dict], int]:
    async with httpx.AsyncClient(timeout=15, follow_redirects=False, headers={"Accept": "application/json", "User-Agent": "LocalPaperLedger/1.0"}) as client:
        response = await client.get("https://api.dexscreener.com/token-profiles/latest/v1")
        response.raise_for_status()
        profiles = response.json()
        if not isinstance(profiles, list):
            raise ScanError("Invalid token profiles response")
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
                raise ScanError("Invalid token pairs response")
            pairs = payload.get("pairs") or []
    observed = db.utc_now()
    # Only a pair's base-token price is priceUsd.
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
            candidates.append(max(options, key=lambda x: D(x["liquidity_usd"]) if x["liquidity_usd"] is not None else D(-1)))
    return candidates, len(tokens)


def _stock_candidate(payload: Any, symbol: str, observed: str) -> dict | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("Global Quote"), dict):
        return None
    quote_data = payload["Global Quote"]
    if quote_data.get("01. symbol") != symbol:
        return None
    price = safe_number(quote_data.get("05. price"), positive=True)
    volume = safe_number(quote_data.get("06. volume"))
    quote_date = quote_data.get("07. latest trading day")
    try:
        if not isinstance(quote_date, str) or date.fromisoformat(quote_date).isoformat() != quote_date:
            return None
    except ValueError:
        return None
    if price is None or volume is None or volume < 0 or volume != volume.to_integral_value():
        return None
    raw_change = quote_data.get("10. change percent")
    change = safe_number(raw_change[:-1]) if isinstance(raw_change, str) and raw_change.endswith("%") else None
    return {"symbol": symbol, "chain": None, "pair_address": None, "price_usd": str(price),
            "liquidity_usd": None, "volume_24h": None, "price_change_24h": None,
            "volume_spike_ratio": None, "pair_created_at": None, "risk_status": "manual only",
            "reason": "Stock quote for manual paper trades only; end-of-day historical data, no liquidity or risk assessment. USD assumed for US-listed stocks; currency is not guaranteed by the provider.",
            "url": None, "observed_at": observed, "asset_type": "stock", "source": "Alpha Vantage",
            "quote_date": quote_date, "stock_volume_shares": int(volume),
            "stock_change_percent": str(change) if change is not None else None, "cache_status": "fresh"}


def _classify_stock_notice(value: Any) -> str:
    """Classify locally; never return or log provider text (which may contain a key)."""
    text = value.lower() if isinstance(value, str) else ""
    key = r"(?:api[ _-]?key|apikey)"
    invalid = r"(?:invalid|incorrect|not valid|not a valid|unauthorized|revoked|missing)"
    if re.search(rf"\b{invalid}(?:\s+or\s+{invalid})?\s+(?:your\s+)?{key}\b", text) or re.search(
        rf"\b{key}(?:\s+(?:parameter|you provided|you supplied|provided|supplied|entered))?\s*(?:is|was|:)\s*{invalid}\b(?!\s+symbol)", text
    ):
        return "auth"
    if (re.search(r"rate[ -]?limit|quota|too many requests|call frequency", text)
        or re.search(r"(?:free|standard).{0,100}(?:requests?|calls?).{0,30}per (?:minute|hour|day)", text)):
        return "quota"
    if re.search(r"premium|entitlement|not entitled|subscription.{0,40}(?:required|need)|(?:requires?|need).{0,40}subscription|not available.{0,40}(?:plan|subscription)", text):
        return "premium"
    if re.search(r"(?:request|call).{0,40}(?:limit|exceed|reached)|(?:limit|exceed|reached).{0,40}(?:request|call)", text):
        return "quota"
    return "unknown"


def _is_stock_symbol_error(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    text = value.lower()
    problem = r"(?:invalid|unknown|unsupported|unrecognized|not found|no data|not supported|check)"
    return bool(re.search(rf"{problem}.{{0,80}}(?:symbol|ticker)|(?:symbol|ticker).{{0,80}}{problem}", text))


_STOCK_NOTICE_MESSAGES = {
    "quota": "Alpha Vantage reported a request quota or rate limit. Retry manually when access is available; no reset time is assumed.",
    "auth": "Alpha Vantage reported an invalid or missing API key. Check the configured market data key.",
    "premium": "Alpha Vantage reported that the request requires different provider access or a premium entitlement. Check your plan.",
    "unknown": "Alpha Vantage returned an unrecognized account or information notice. Check the provider account; its cause is unknown.",
}


def _cached_stock_candidate(row: Any) -> dict | None:
    # Validate persisted values again, and never invent an observation time.
    if db.parse_timestamp(row["observed_at"]) is None:
        return None
    change = row["stock_change_percent"]
    payload = {"Global Quote": {"01. symbol": row["symbol"], "05. price": row["price_usd"],
               "06. volume": row["stock_volume_shares"], "07. latest trading day": row["quote_date"],
               "10. change percent": f"{change}%" if change is not None else None}}
    item = _stock_candidate(payload, row["symbol"], row["observed_at"])
    if item and change is not None and item["stock_change_percent"] is None:
        return None
    return item


async def _scan_stocks(symbols: list[str]) -> tuple[list[dict], list[str]]:
    warnings: list[str] = []
    results: dict[str, dict] = {}
    cache: dict[str, dict] = {}
    with db.read_connection() as conn:
        for symbol, row in db.get_stock_quote_cache(conn, provider=db.ALPHA_VANTAGE_QUOTE_CACHE, symbols=symbols).items():
            item = _cached_stock_candidate(row)
            if item:
                cache[symbol] = item
    now = datetime.now(timezone.utc)
    pending = []
    for symbol in symbols:
        item = cache.get(symbol)
        if item and _stock_cache_is_fresh(item["observed_at"], now):
            results[symbol] = {**item, "cache_status": "cached"}
        else:
            pending.append(symbol)

    if pending:
        try:
            key = market_data.load_key()
        except HTTPException:
            key = None
            warnings.append("Alpha Vantage market key is unavailable or cannot be decrypted; check encryption configuration.")
            warnings.extend(f"{symbol}: skipped because the market key is unavailable." for symbol in pending)
            pending = []
        else:
            if key is None:
                if "IBM" in pending:
                    warnings.append("Alpha Vantage demo key is used for IBM only; quotes are end-of-day historical, not real-time.")
                for symbol in pending:
                    if symbol != "IBM":
                        warnings.append(f"{symbol}: skipped; configure your own Alpha Vantage API key; the demo key is only available for IBM.")
                pending = [symbol for symbol in pending if symbol == "IBM"]
                key = "demo"
        if pending:
            # One request per missing/expired symbol, sequentially, no retries.
            deadline = asyncio.get_running_loop().time() + 30
            async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False,
                                         headers={"Accept": "application/json", "User-Agent": "LocalPaperLedger/1.0"}) as client:
                for index, symbol in enumerate(pending):
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        warnings.extend(f"{s}: skipped after stock scan time limit." for s in pending[index:])
                        break
                    stop = False
                    try:
                        response = await asyncio.wait_for(client.get("https://www.alphavantage.co/query", params={"function": "GLOBAL_QUOTE", "symbol": symbol, "apikey": key}), timeout=remaining)
                        response.raise_for_status()
                        payload = response.json()
                        notice_key = next((k for k in ("Note", "Information") if isinstance(payload, dict) and k in payload), None)
                        has_error = isinstance(payload, dict) and "Error Message" in payload
                        error_message = payload.get("Error Message") if has_error else None
                        kind = _classify_stock_notice(payload[notice_key] if notice_key else error_message)
                        if notice_key or (has_error and (kind != "unknown" or not _is_stock_symbol_error(error_message))):
                            warnings.append(f"{symbol}: {_STOCK_NOTICE_MESSAGES[kind]}")
                            stop = True
                        elif has_error:
                            warnings.append(f"{symbol}: Alpha Vantage rejected the symbol or request.")
                        else:
                            item = _stock_candidate(payload, symbol, db.utc_now())
                            if item is None:
                                warnings.append(f"{symbol}: Alpha Vantage returned an empty or invalid quote.")
                            else:
                                results[symbol] = item
                    except httpx.HTTPStatusError as exc:
                        status = exc.response.status_code
                        if status == 429:
                            warnings.append(f"{symbol}: {_STOCK_NOTICE_MESSAGES['quota']} (HTTP 429.)")
                            stop = True
                        else:
                            try:
                                body = exc.response.json()
                            except (ValueError, TypeError):
                                body = None
                            notice_key = next((k for k in ("Note", "Information", "Error Message") if isinstance(body, dict) and k in body), None)
                            notice = body[notice_key] if notice_key else None
                            kind = _classify_stock_notice(notice)
                            if notice_key and (notice_key != "Error Message" or kind != "unknown" or not _is_stock_symbol_error(notice)):
                                warnings.append(f"{symbol}: {_STOCK_NOTICE_MESSAGES[kind]} (HTTP {status}.)")
                                stop = True
                            elif status in (401, 403):
                                # An authorization status alone does not establish an invalid key.
                                warnings.append(f"{symbol}: Alpha Vantage refused the request (HTTP {status}); check provider account access.")
                                stop = True
                            else:
                                warnings.append(f"{symbol}: Alpha Vantage returned HTTP {status}.")
                                stop = 300 <= status < 400
                    except (asyncio.TimeoutError, httpx.TimeoutException):
                        warnings.append(f"{symbol}: Alpha Vantage request timed out.")
                    except Exception:
                        # Never include exception details, upstream bodies, or query URLs.
                        warnings.append(f"{symbol}: Alpha Vantage request failed or returned invalid data.")
                    if stop:
                        warnings.extend(f"{s}: skipped after provider notice to avoid further requests." for s in pending[index + 1:])
                        break

    for symbol in symbols:
        if symbol not in results and symbol in cache:
            results[symbol] = {**cache[symbol], "cache_status": "stale"}
            warnings.append(f"{symbol}: showing a stale cached stock quote; the original observation time is retained and the quote was not refreshed.")
        elif symbol in results and results[symbol]["cache_status"] == "cached" and not _stock_cache_is_fresh(results[symbol]["observed_at"]):
            results[symbol]["cache_status"] = "stale"
            warnings.append(f"{symbol}: the cached stock quote expired during this scan; the original observation time is retained.")
    candidates = [results[symbol] for symbol in symbols if symbol in results]
    if candidates and not any(item["cache_status"] == "fresh" for item in candidates):
        warnings.append("Only cached stock quotes are available; no new stock quotes were fetched in this scan. Check each cache status and observation time.")
    return candidates, warnings


_CANDIDATE_FIELDS = ("symbol", "chain", "pair_address", "price_usd", "liquidity_usd", "volume_24h",
    "price_change_24h", "volume_spike_ratio", "pair_created_at", "risk_status", "reason", "url",
    "observed_at", "asset_type", "source", "quote_date", "stock_volume_shares", "stock_change_percent", "cache_status")
_DECIMAL_CANDIDATE_FIELDS = ("price_usd", "liquidity_usd", "volume_24h", "price_change_24h", "volume_spike_ratio", "stock_change_percent")


async def scan_market() -> dict[str, Any]:
    async with _scan_lock:
        with db.read_connection() as conn:
            settings = db.get_settings(conn)
        mode = settings["market_mode"]
        candidates, warnings, profiles_scanned = [], [], 0
        successful_sources = 0
        if mode in ("crypto", "both"):
            try:
                crypto, profiles_scanned = await _scan_crypto(settings)
                candidates.extend(crypto)
                successful_sources += 1
            except (httpx.HTTPError, OSError, ValueError, TypeError, ScanError):
                warnings.append("DexScreener scan failed; no crypto candidates from this scan.")
        if mode in ("stocks", "both"):
            stocks, stock_warnings = await _scan_stocks(settings["stock_symbols"])
            candidates.extend(stocks)
            warnings.extend(stock_warnings)
            if stocks:
                successful_sources += 1
        observed = db.utc_now()
        if not successful_sources:
            detail = "All requested market sources failed; previously saved candidates may be stale. " + " ".join(warnings)
            with db.transaction() as conn:
                db.set_agent(conn, "scanner", "error", detail)
                db.log(conn, "scanner", detail, "error")
            raise ScanError(detail)
        with db.transaction() as conn:
            current = db.get_settings(conn)
            changed = current["market_mode"] != mode or (mode in ("stocks", "both") and current["stock_symbols"] != settings["stock_symbols"])
            if changed:
                detail = "Market settings changed during scan; no candidates were saved or traded. Scan again."
                db.set_agent(conn, "scanner", "error", detail)
                db.log(conn, "scanner", detail, "error")
            else:
                conn.execute("DELETE FROM candidates")
                for item in candidates:
                    if item["asset_type"] == "stock" and item["cache_status"] == "fresh":
                        db.put_stock_quote_cache(conn, provider=db.ALPHA_VANTAGE_QUOTE_CACHE, item=item)
                    # Legacy chain/pair columns are NOT NULL. Empty storage values are
                    # represented as null in the API for stock rows.
                    values = [item[k] if k not in ("chain", "pair_address") else item[k] or "" for k in _CANDIDATE_FIELDS]
                    conn.execute("INSERT INTO candidates(" + ",".join(_CANDIDATE_FIELDS) + ") VALUES (" + ",".join("?" for _ in _CANDIDATE_FIELDS) + ")", values)
                    if item["asset_type"] == "crypto" and item["price_usd"] is not None:
                        conn.execute("UPDATE positions SET mark_price=?,mark_source='dexscreener',mark_timestamp=? WHERE asset_type='crypto' AND chain=? AND pair_address=?", (item["price_usd"], item["observed_at"], item["chain"], item["pair_address"]))
                db.snapshot(conn)
                eligible = sum(c["risk_status"] == "eligible" for c in candidates)
                detail = f"Market scan ({mode}): {profiles_scanned} crypto profiles, {len(candidates)} candidates, {eligible} eligible for crypto paper cycles; {len(warnings)} warning(s)."
                db.set_agent(conn, "scanner", "warning" if warnings else "ready", detail)
                db.set_agent(conn, "rug_check", "ready", "Crypto liquidity/volume checks only; cannot identify scams, honeypots or rug pulls. Stocks are manual only.")
                db.log(conn, "scanner", detail, "warning" if warnings else "info")
        if changed:
            raise ScanError(detail)
        return {"observed_at": observed, "profiles_scanned": profiles_scanned, "market_mode": mode,
                "warnings": warnings, "candidates": [{**c, **{k: _f(c[k]) for k in _DECIMAL_CANDIDATE_FIELDS}} for c in candidates]}


def auto_buy(candidate: dict[str, Any]) -> dict[str, Any]:
    with localcontext() as ctx:
        ctx.prec = 50
        with db.transaction() as conn:
            settings = db.get_settings(conn)
            if not settings["auto_paper"]:
                raise TradeError("Auto-paper is disabled.")
            if candidate.get("asset_type", "crypto") != "crypto" or settings["market_mode"] not in ("crypto", "both"):
                raise TradeError("Automatic paper orders are restricted to crypto in the current market mode.")
            scan_status = conn.execute("SELECT status FROM agents WHERE id='scanner'").fetchone()
            saved = conn.execute("SELECT * FROM candidates WHERE asset_type='crypto' AND symbol=? AND chain=? AND pair_address=? AND observed_at=?", (candidate["symbol"], candidate["chain"], candidate["pair_address"], candidate["observed_at"])).fetchone()
            if not scan_status or scan_status[0] == "error" or saved is None:
                raise TradeError("Candidate is stale or superseded by a later scan; scan again.")
            # Trust persisted scan data, not a caller-modified candidate payload.
            candidate = dict(saved)
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
