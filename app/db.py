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
    """打开一个连接（启用外键与 WAL）。"""
    raise NotImplementedError


def init_db(conn: sqlite3.Connection) -> None:
    """创建全部表与索引。"""
    raise NotImplementedError
