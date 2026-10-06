"""SQLite 连接管理与建表。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS accounts (
    account_id TEXT PRIMARY KEY,
    balance    INTEGER NOT NULL CHECK (balance >= 0),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    seq            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id     TEXT NOT NULL,
    event_type     TEXT NOT NULL,
    amount         INTEGER NOT NULL CHECK (amount >= 0),
    balance_after  INTEGER NOT NULL CHECK (balance_after >= 0),
    transfer_id    TEXT,
    timestamp      TEXT NOT NULL,
    FOREIGN KEY (account_id) REFERENCES accounts(account_id)
);
CREATE INDEX IF NOT EXISTS idx_events_account ON events(account_id, seq);

CREATE TABLE IF NOT EXISTS idempotency (
    idempotency_key TEXT PRIMARY KEY,
    operation       TEXT NOT NULL,
    request_hash    TEXT NOT NULL,
    response_json   TEXT NOT NULL,
    status          TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """打开连接：WAL 提高并发读写能力，开启外键，行映射为 Row。"""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(path),
        timeout=30.0,           # 锁等待超时，避免 BUSY 立刻报错
        isolation_level=None,   # 手动事务，BEGIN IMMEDIATE 由仓储控制
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=FULL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """幂等建表。"""
    conn.executescript(SCHEMA_SQL)
