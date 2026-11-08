"""SQLite persistence for a single-user paper ledger. No broker integration exists."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
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
CREATE TABLE IF NOT EXISTS agents (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL
);
"""

_init_lock = threading.Lock()
_initialized_paths: set[str] = set()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


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
            "stock_change_percent": "TEXT",
        }
        for name, column_type in migrations.items():
            if name not in candidate_columns:
                conn.execute(f"ALTER TABLE candidates ADD COLUMN {name} {column_type}")
        created = conn.execute("INSERT OR IGNORE INTO settings(id, data) VALUES (1, ?)", (json.dumps(DEFAULT_SETTINGS),)).rowcount
        conn.execute("INSERT OR IGNORE INTO account(id, cash) VALUES (1, ?)", (str(DEFAULT_SETTINGS["starting_balance"]),))
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
