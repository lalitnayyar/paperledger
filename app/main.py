"""Local-only paper trading API. This application never places live orders."""
from __future__ import annotations

from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

from app import db, trading, yahoo_quotes
from app.llm import router as llm_router
from app.market_data import router as market_data_router

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.ensure_initialized()
    yield


# JSON may escape each CSV byte as up to six ASCII characters (e.g. \u0000).
# Leave modest envelope space while bounding input before Pydantic parses it.
STOCK_IMPORT_MAX_REQUEST_BYTES = 6 * yahoo_quotes.MAX_IMPORT_BYTES + 1024


class StockImportBodyLimitMiddleware:
    """Limit this request's streamed body without trusting Content-Length."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] != "/api/stock-import" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return
        chunks, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > STOCK_IMPORT_MAX_REQUEST_BYTES:
                response = JSONResponse(status_code=422, content={"detail": "CSV request exceeds the size limit."}, headers={"Cache-Control": "no-store"})
                await response(scope, receive, send)
                return
            if chunk:
                chunks.append(chunk)
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


app = FastAPI(title="TradeLedger Paper Trading", lifespan=lifespan)
app.add_middleware(StockImportBodyLimitMiddleware)
app.include_router(llm_router)
app.include_router(market_data_router)


@app.middleware("http")
async def local_origin_policy(request: Request, call_next):
    # No permissive CORS: browser writes must originate from the same host.
    # This is CSRF defense in depth, not user authentication or a firewall.
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin:
            parsed = urlsplit(origin)
            if parsed.scheme not in ("http", "https") or parsed.netloc.lower() != request.headers.get("host", "").lower() or parsed.path not in ("", "/"):
                return JSONResponse(status_code=403, content={"detail": "Cross-origin requests are not allowed."})
    if request.url.path.startswith("/api/"):
        try:
            # Also supports ASGI test clients that do not enter lifespan.
            db.ensure_initialized()
        except Exception:
            return JSONResponse(status_code=503, content={"detail": "Local database initialization failed."})
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    # Never echo raw inputs (NaN/Infinity also break JSON encoding and could leak secrets).
    if request.url.path == "/api/stock-import":
        # Unknown field names and validation locations may themselves contain user data.
        return JSONResponse(status_code=422, content={"detail": "Provide a CSV string within the import size limit."})
    errors = [{"loc": list(item.get("loc", ())), "msg": item.get("msg", "Invalid input"), "type": item.get("type", "value_error")} for item in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": errors})


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class SettingsUpdate(StrictModel):
    starting_balance: float | None = Field(default=None, gt=0, le=1e15)
    risk_percent: float | None = Field(default=None, gt=0, le=100)
    max_position_percent: float | None = Field(default=None, gt=0, le=100)
    min_liquidity: float | None = Field(default=None, ge=0, le=1e15)
    min_volume: float | None = Field(default=None, ge=0, le=1e15)
    slippage_bps: float | None = Field(default=None, ge=0, le=5000)
    fee_bps: float | None = Field(default=None, ge=0, le=5000)
    stop_loss_percent: float | None = Field(default=None, gt=0, le=100)
    take_profit_percent: float | None = Field(default=None, gt=0, le=10000)
    auto_paper: StrictBool | None = None
    scan_interval_seconds: int | None = Field(default=None, ge=10, le=86400, strict=True)
    market_mode: Literal["crypto", "stocks", "both"] | None = None
    stock_symbols: list[str] | None = Field(default=None, min_length=1, max_length=25)

    @field_validator("stock_symbols", mode="before")
    @classmethod
    def normalize_stock_symbols(cls, values):
        if values is None:
            return None
        if not isinstance(values, list):
            raise ValueError("Stock symbols must be a list")
        symbols = []
        for value in values:
            if not isinstance(value, str) or not value.isascii():
                raise ValueError("Stock symbols must use US ticker syntax")
            symbol = value.strip().upper()
            if not trading.STOCK_SYMBOL_RE.fullmatch(symbol):
                raise ValueError("Use US-listed ticker syntax, such as IBM or BRK.B")
            symbols.append(symbol)
        if len(set(symbols)) != len(symbols):
            raise ValueError("Stock symbols must not contain duplicates")
        return symbols


class StockImport(StrictModel):
    # Match the parser's limit in both characters and UTF-8 bytes. No coercion.
    csv: str = Field(strict=True, max_length=yahoo_quotes.MAX_IMPORT_BYTES)

    @field_validator("csv")
    @classmethod
    def bounded_utf8_csv(cls, value: str) -> str:
        try:
            size = len(value.encode("utf-8"))
        except UnicodeEncodeError:
            raise ValueError("CSV must be valid UTF-8.") from None
        if size > yahoo_quotes.MAX_IMPORT_BYTES:
            raise ValueError("Import exceeds the size limit.")
        return value


class ManualTrade(StrictModel):
    symbol: str = Field(min_length=1, max_length=24)
    asset_type: Literal["stock", "crypto"]
    side: Literal["BUY", "SELL"]
    quantity: Decimal = Field(gt=0, le=Decimal("1e15"), allow_inf_nan=False)
    price: Decimal = Field(gt=0, le=Decimal("1e15"), allow_inf_nan=False)
    reason: str = Field(min_length=1, max_length=300)

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value):
        if not isinstance(value, str):
            raise ValueError("Symbol must be text")
        symbol = value.strip().upper()
        if not trading.SYMBOL_RE.fullmatch(symbol):
            raise ValueError("Use 1–24 letters, digits, dots, hyphens or underscores in the symbol")
        return symbol

    @field_validator("asset_type", mode="before")
    @classmethod
    def normalize_asset(cls, value):
        return value.lower().strip() if isinstance(value, str) else value

    @field_validator("side", mode="before")
    @classmethod
    def normalize_side(cls, value):
        return value.upper().strip() if isinstance(value, str) else value

    @field_validator("quantity", "price")
    @classmethod
    def limit_decimal_places(cls, value):
        if value.as_tuple().exponent < -18:
            raise ValueError("At most 18 decimal places are supported")
        return value

    @field_validator("reason")
    @classmethod
    def valid_reason(cls, value):
        clean = db.clean_text(value, 300)
        if not clean:
            raise ValueError("A trade reason is required")
        return clean


class Mark(StrictModel):
    price: Decimal = Field(gt=0, le=Decimal("1e15"), allow_inf_nan=False)

    @field_validator("price")
    @classmethod
    def limit_decimal_places(cls, value):
        if value.as_tuple().exponent < -18:
            raise ValueError("At most 18 decimal places are supported")
        return value


@app.get("/api/health")
def health():
    return {"status": "ok", "mode": "paper-only", "database": "ready", "continuous_worker": "off"}


@app.get("/api/dashboard")
def dashboard():
    data = trading.dashboard()
    data["disclosures"] = {
        "market_data": "DexScreener shows recently profiled tokens, not necessarily new pairs. Alpha Vantage GLOBAL_QUOTE is end-of-day historical data, not real-time: quote_date is its trading date, stock_volume_shares is shares, and stock_change_percent is close-to-close. Enter US-listed USD stocks only; the provider does not guarantee currency or verify US listing. With no key, the demo is available only for IBM. Manual scans reuse stock quotes for six hours and request only missing or expired symbols, subject to provider limits; stale cached quotes may be shown when refresh fails. Stock quotes are manual paper trades only and do not update marks. Check cache status, trading dates and original observation times.",
        "manual_import": "Yahoo Finance imports are user-provided, not independently verified, and may be stale. The quote date is optional and user-supplied; the recorded import time is not the quote time. USD and US listing are assumed, not verified. Manual paper trading only: imports never trigger automatic buys or position marks.",
        "risk": "Basic price, liquidity and volume checks do not detect scams, honeypots, rug pulls, or ensure safety.",
        "marks": "Unmarked positions make total equity and total unrealized P&L unavailable. Entry price includes allocated buy fees.",
        "orders": "Paper simulation only; no live orders. No automated stop-loss or take-profit execution; stop is a sizing assumption and take-profit is advisory.",
        "worker": "No background worker is running; scan_interval_seconds is reserved for future use.",
        "closed_trades": "Closed trades and win rate count individual sell executions, including partial sells.",
    }
    return data


@app.post("/api/stock-import")
def stock_import(payload: StockImport):
    candidates, warnings = yahoo_quotes.import_quotes(payload.csv)
    if not candidates:
        # Parser warnings are fixed/sanitized; omit the general disclosure and
        # report one brief reason without exposing the CSV or rejected values.
        reason = warnings[-1] if len(warnings) > 1 else "No valid quote rows were supplied."
        raise HTTPException(422, f"No quotes imported. {reason}")
    db.replace_manual_stock_quotes(candidates)
    warnings.append("Quote dates are optional and user-supplied; import time is not quote time. Manual paper trading only; no automatic buys or position marks.")
    return {"imported": len(candidates), "warnings": warnings}


@app.put("/api/settings")
def settings(payload: SettingsUpdate):
    patch = payload.model_dump(exclude_unset=True)
    if not patch:
        raise HTTPException(422, "Provide at least one setting.")
    if any(value is None for value in patch.values()):
        raise HTTPException(422, "Settings cannot be null.")
    try:
        return trading.update_settings(patch)
    except trading.TradeError as exc:
        raise HTTPException(409, str(exc)) from None


@app.post("/api/trades")
def trade(payload: ManualTrade):
    try:
        return trading.manual_trade(**payload.model_dump())
    except trading.TradeError as exc:
        raise HTTPException(409, str(exc)) from None


@app.patch("/api/positions/{position_id}/mark")
def mark(position_id: int, payload: Mark):
    if position_id <= 0:
        raise HTTPException(404, "Position not found.")
    try:
        return trading.mark_position(position_id, payload.price)
    except trading.TradeError as exc:
        raise HTTPException(404, str(exc)) from None


@app.post("/api/scan")
async def scan():
    try:
        return await trading.scan_market()
    except trading.ScanError as exc:
        raise HTTPException(502, str(exc)) from None


@app.post("/api/cycle")
async def cycle():
    try:
        scan_result = await trading.scan_market()
    except trading.ScanError as exc:
        raise HTTPException(502, str(exc)) from None
    with db.read_connection() as conn:
        enabled = db.get_settings(conn)["auto_paper"]
    trades, skipped = [], []
    if enabled:
        for candidate in scan_result["candidates"]:
            if candidate.get("asset_type", "crypto") != "crypto" or candidate["risk_status"] != "eligible":
                continue
            # Bound one manual cycle to at most three new positions.
            if len(trades) >= 3:
                skipped.append({"symbol": candidate["symbol"], "reason": "Three-buy per cycle limit reached."})
                continue
            try:
                trades.append(trading.auto_buy(candidate))
            except trading.TradeError as exc:
                skipped.append({"symbol": candidate["symbol"], "reason": str(exc)})
    with db.transaction() as conn:
        if skipped:
            db.log(conn, "sizer", f"Cycle skipped {len(skipped)} candidate(s); see cycle response for reasons.", "warning")
        db.log(conn, "executor", f"Manual cycle completed; auto-paper {'enabled' if enabled else 'disabled'}; {len(trades)} paper buy(s).")
    return {**scan_result, "auto_paper": enabled, "trades": trades, "skipped": skipped,
            "message": "Paper only; stops and take-profit are not automatic. No background worker is running."}


@app.get("/")
def index():
    index_file = STATIC_DIR / "index.html"
    if not index_file.is_file():
        raise HTTPException(503, "Dashboard assets are not available.")
    return FileResponse(index_file)


app.mount("/static", StaticFiles(directory=str(STATIC_DIR), check_dir=False), name="static")
