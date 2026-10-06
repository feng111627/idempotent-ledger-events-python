"""SQLite 连接管理与建表。

并发模型：
- 每个工作线程持有自己的连接（threading.local），避免多线程共享
  同一连接导致事务交错；
- 写事务一律 BEGIN IMMEDIATE，写者在数据库层串行；配合 busy_timeout，
  后来的写者等待而不是立刻报 BUSY；
- WAL 模式下读者不阻塞写者、写者不阻塞读者。
"""
from __future__ import annotations

import sqlite3
import threading
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


class Database:
    """持有数据库路径，按线程惰性提供独立连接。"""

    def __init__(self, db_path: str | Path) -> None:
        self.path = str(db_path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()

    def get_conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = connect(self.path)
            self._local.conn = conn
        return conn

    def init(self) -> None:
        init_db(self.get_conn())

    def close_current(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None


def connect(db_path: str | Path) -> sqlite3.Connection:
    """打开连接：WAL、外键、行工厂、手动事务。"""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(path),
        timeout=30.0,           # 锁等待超时
        isolation_level=None,   # 手动事务（BEGIN IMMEDIATE 由仓储控制）
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
