"""SQLite persistence for a single-user paper ledger. No broker integration exists."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterator

DEFAULT_SETTINGS = {
    "starting_balance": 100000.0,
    "risk_percent": 1.0,
    "max_position_percent": 10.0,
    "min_liquidity": 50000.0,
    "min_volume": 10000.0,
    "slippage_bps": 10.0,
    "fee_bps": 10.0,
    "stop_loss_percent": 10.0,
    "take_profit_percent": 20.0,
    "auto_paper": False,
    "scan_interval_seconds": 300,
    "market_mode": "crypto",
    "stock_symbols": ["IBM"],
    "stock_provider": "alpha_vantage",
    "stock_source_mode": "both",
}

AGENTS = {
    "scanner": ("Scanner", "idle", "Manual scan only; no market data fetched yet."),
    "rug_check": ("Risk / liquidity check", "idle", "Liquidity and volume checks only; not rug-pull detection."),
    "sizer": ("Position sizer", "idle", "Sizes eligible paper buys using marked equity and configured risk."),
    "executor": ("Paper executor", "idle", "Simulation only; no brokerage connection or live orders."),
    "logger": ("Activity logger", "ready", "Local SQLite audit trail."),
    "equity_curve": ("Equity curve", "ready", "Snapshots use cash plus explicitly marked positions."),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id = 1), data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS account (id INTEGER PRIMARY KEY CHECK(id = 1), cash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS positions (
 id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, asset_type TEXT NOT NULL,
 quantity TEXT NOT NULL, cost_basis TEXT NOT NULL, mark_price TEXT,
 mark_source TEXT, mark_timestamp TEXT, chain TEXT, pair_address TEXT,
 opened_at TEXT NOT NULL, UNIQUE(symbol, asset_type)
);
CREATE TABLE IF NOT EXISTS trades (
 id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, symbol TEXT NOT NULL,
 side TEXT NOT NULL, asset_type TEXT NOT NULL, quantity TEXT NOT NULL,
 reference_price TEXT NOT NULL, fill_price TEXT NOT NULL, slippage_bps TEXT NOT NULL,
 fee TEXT NOT NULL, realized_pnl TEXT, reason TEXT NOT NULL,
 chain TEXT, pair_address TEXT
);
CREATE TABLE IF NOT EXISTS equity_snapshots (
 id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, equity TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS activity (
 id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, agent TEXT NOT NULL,
 message TEXT NOT NULL, level TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candidates (
 id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, chain TEXT NOT NULL,
 pair_address TEXT NOT NULL, price_usd TEXT, liquidity_usd TEXT, volume_24h TEXT,
 price_change_24h TEXT, volume_spike_ratio TEXT, pair_created_at TEXT,
 risk_status TEXT NOT NULL, reason TEXT NOT NULL, url TEXT,
 observed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS manual_stock_quotes (
 symbol TEXT NOT NULL PRIMARY KEY, price_usd TEXT NOT NULL, quote_date TEXT,
 stock_volume_shares INTEGER, observed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agents (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL
);
"""

_init_lock = threading.Lock()
_initialized_paths: set[str] = set()
ALPHA_VANTAGE_QUOTE_CACHE = "alpha_vantage:global_quote"
FINNHUB_QUOTE_CACHE = "finnhub:quote"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_timestamp(value: Any) -> datetime | None:
    """Parse only timezone-aware ISO timestamps; never guess a missing timezone."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.utcoffset() is not None else None
    except (ValueError, OverflowError):
        return None


def clean_text(value: Any, limit: int = 300) -> str:
    # Flatten control characters to prevent multiline or terminal escape log injection.
    return " ".join("".join(c if c.isprintable() else " " for c in str(value)).split())[:limit]


def data_path() -> Path:
    return Path(os.environ.get("DATA_DIR", "./data")) / "tradeledger.sqlite3"


def connect() -> sqlite3.Connection:
    path = data_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 15000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    conn = connect()
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        conn.execute("BEGIN IMMEDIATE")
        candidate_columns = {row[1] for row in conn.execute("PRAGMA table_info(candidates)")}
        migrations = {
            "volume_spike_ratio": "TEXT", "pair_created_at": "TEXT",
            "asset_type": "TEXT NOT NULL DEFAULT 'crypto'",
            "source": "TEXT NOT NULL DEFAULT 'DexScreener'",
            "quote_date": "TEXT", "stock_volume_shares": "INTEGER",
            "stock_change_percent": "TEXT", "cache_status": "TEXT",
            "quote_timestamp": "TEXT",
        }
        for name, column_type in migrations.items():
            if name not in candidate_columns:
                conn.execute(f"ALTER TABLE candidates ADD COLUMN {name} {column_type}")
        cache_existed = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='stock_quote_cache'").fetchone() is not None
        old_cache = cache_existed and "provider" not in {row[1] for row in conn.execute("PRAGMA table_info(stock_quote_cache)")}
        if old_cache:
            conn.execute("ALTER TABLE stock_quote_cache RENAME TO stock_quote_cache_legacy")
        conn.execute("""CREATE TABLE IF NOT EXISTS stock_quote_cache (
            provider TEXT NOT NULL, symbol TEXT NOT NULL, price_usd TEXT NOT NULL, quote_date TEXT,
            stock_volume_shares INTEGER, stock_change_percent TEXT, observed_at TEXT NOT NULL,
            PRIMARY KEY(provider, symbol)
        )""")
        cache_columns = {row[1] for row in conn.execute("PRAGMA table_info(stock_quote_cache)")}
        if "quote_timestamp" not in cache_columns:
            conn.execute("ALTER TABLE stock_quote_cache ADD COLUMN quote_timestamp TEXT")
        if old_cache:
            legacy_columns = {row[1] for row in conn.execute("PRAGMA table_info(stock_quote_cache_legacy)")}
            legacy_timestamp = "quote_timestamp" if "quote_timestamp" in legacy_columns else "NULL"
            conn.execute(f"""INSERT INTO stock_quote_cache(provider,symbol,price_usd,quote_date,stock_volume_shares,stock_change_percent,observed_at,quote_timestamp)
                SELECT ?,symbol,price_usd,quote_date,stock_volume_shares,stock_change_percent,observed_at,{legacy_timestamp} FROM stock_quote_cache_legacy""",
                (ALPHA_VANTAGE_QUOTE_CACHE,))
            conn.execute("DROP TABLE stock_quote_cache_legacy")
        created = conn.execute("INSERT OR IGNORE INTO settings(id, data) VALUES (1, ?)", (json.dumps(DEFAULT_SETTINGS),)).rowcount
        conn.execute("INSERT OR IGNORE INTO account(id, cash) VALUES (1, ?)", (str(DEFAULT_SETTINGS["starting_balance"]),))
        if not cache_existed:
            # Seed once, only from valid Alpha Vantage candidate records for the
            # current watchlist. Preserve the actual observation time, even if old.
            watched = set(get_settings(conn)["stock_symbols"])
            now = datetime.now(timezone.utc)
            for row in conn.execute("SELECT * FROM candidates WHERE asset_type='stock' AND source='Alpha Vantage' ORDER BY id DESC"):
                try:
                    symbol, observed = row["symbol"], parse_timestamp(row["observed_at"])
                    price, volume = Decimal(str(row["price_usd"])), Decimal(str(row["stock_volume_shares"]))
                    day = row["quote_date"]
                    change = Decimal(str(row["stock_change_percent"])) if row["stock_change_percent"] is not None else None
                    valid_number = lambda n: n.is_finite() and abs(n) <= Decimal("1e18") and n.as_tuple().exponent >= -18
                    if (symbol not in watched or not re.fullmatch(r"[A-Z]{1,5}(?:[.-][A-Z]{1,4})?", symbol)
                        or observed is None or observed > now or not isinstance(day, str)
                        or date.fromisoformat(day).isoformat() != day or not valid_number(price) or price <= 0
                        or not valid_number(volume) or volume < 0 or volume != volume.to_integral_value()
                        or (change is not None and not valid_number(change))):
                        continue
                    conn.execute("INSERT OR IGNORE INTO stock_quote_cache(provider,symbol,price_usd,quote_date,stock_volume_shares,stock_change_percent,observed_at) VALUES (?,?,?,?,?,?,?)",
                                 (ALPHA_VANTAGE_QUOTE_CACHE, symbol, str(price), day, int(volume), str(change) if change is not None else None, row["observed_at"]))
                except (ValueError, TypeError, InvalidOperation, OverflowError):
                    continue
            conn.execute("UPDATE candidates SET cache_status='cached' WHERE asset_type='stock' AND source='Alpha Vantage' AND cache_status IS NULL")
        for agent_id, (name, status, detail) in AGENTS.items():
            conn.execute("INSERT OR IGNORE INTO agents(id,name,status,detail) VALUES (?,?,?,?)", (agent_id, name, status, detail))
        if created:
            conn.execute("INSERT INTO equity_snapshots(timestamp,equity) VALUES (?,?)", (utc_now(), str(DEFAULT_SETTINGS["starting_balance"])))
            log(conn, "logger", "Paper ledger initialized. No live trading is available.")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ensure_initialized() -> None:
    path = str(data_path().absolute())
    if path in _initialized_paths and Path(path).exists():
        return
    with _init_lock:
        if path not in _initialized_paths or not Path(path).exists():
            init_db()
            _initialized_paths.add(path)


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        # SQLite write lock acquired before checking balances and positions.
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def read_connection() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        # Snapshot all queries within a dashboard response consistently.
        conn.execute("BEGIN")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_settings(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute("SELECT data FROM settings WHERE id=1").fetchone()
    if row is None:
        raise RuntimeError("Database has not been initialized")
    return {**DEFAULT_SETTINGS, **json.loads(row[0])}


def replace_manual_stock_quotes(items: list[dict[str, Any]]) -> None:
    """Atomically replace a nonempty parser-validated import; never store CSV or JSON."""
    if not items:
        raise ValueError("Cannot replace manual quotes with an empty import.")
    with transaction() as conn:
        conn.execute("DELETE FROM manual_stock_quotes")
        conn.executemany(
            "INSERT INTO manual_stock_quotes(symbol,price_usd,quote_date,stock_volume_shares,observed_at) VALUES (?,?,?,?,?)",
            [(item["symbol"], item["price_usd"], item["quote_date"], item["stock_volume_shares"], item["observed_at"]) for item in items],
        )
        log(conn, "scanner", f"Manual stock import replaced: {len(items)} quote(s).")


def get_stock_quote_cache(conn: sqlite3.Connection, *, provider: str, symbols: list[str]) -> dict[str, sqlite3.Row]:
    """Read raw quotes from one provider/endpoint namespace; adapters validate them."""
    if not symbols:
        return {}
    placeholders = ",".join("?" for _ in symbols)
    return {row["symbol"]: row for row in conn.execute(
        f"SELECT * FROM stock_quote_cache WHERE provider=? AND symbol IN ({placeholders})", (provider, *symbols))}


def put_stock_quote_cache(conn: sqlite3.Connection, *, provider: str, item: dict[str, Any]) -> None:
    """Upsert a validated quote in an explicit namespace, within the caller's transaction."""
    conn.execute("""INSERT INTO stock_quote_cache(provider,symbol,price_usd,quote_date,stock_volume_shares,stock_change_percent,observed_at,quote_timestamp)
        VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(provider,symbol) DO UPDATE SET
        price_usd=excluded.price_usd,quote_date=excluded.quote_date,
        stock_volume_shares=excluded.stock_volume_shares,stock_change_percent=excluded.stock_change_percent,
        observed_at=excluded.observed_at,quote_timestamp=excluded.quote_timestamp""",
        (provider, item["symbol"], item["price_usd"], item.get("quote_date"), item.get("stock_volume_shares"),
         item.get("stock_change_percent"), item["observed_at"], item.get("quote_timestamp")))


def set_agent(conn: sqlite3.Connection, agent_id: str, status: str, detail: str) -> None:
    conn.execute("UPDATE agents SET status=?, detail=? WHERE id=?", (clean_text(status, 30), clean_text(detail), agent_id))


def log(conn: sqlite3.Connection, agent: str, message: str, level: str = "info") -> None:
    conn.execute("INSERT INTO activity(timestamp,agent,message,level) VALUES (?,?,?,?)", (utc_now(), clean_text(agent, 40), clean_text(message, 500), level))


def marked_equity(conn: sqlite3.Connection) -> tuple[Decimal | None, Decimal | None]:
    cash = Decimal(conn.execute("SELECT cash FROM account WHERE id=1").fetchone()[0])
    equity, unrealized = cash, Decimal(0)
    for pos in conn.execute("SELECT quantity,cost_basis,mark_price FROM positions"):
        if pos["mark_price"] is None:
            return None, None
        value = Decimal(pos["quantity"]) * Decimal(pos["mark_price"])
        equity += value
        unrealized += value - Decimal(pos["cost_basis"])
    return equity, unrealized


def snapshot(conn: sqlite3.Connection) -> None:
    equity, _ = marked_equity(conn)
    if equity is not None:
        conn.execute("INSERT INTO equity_snapshots(timestamp,equity) VALUES (?,?)", (utc_now(), str(equity)))
