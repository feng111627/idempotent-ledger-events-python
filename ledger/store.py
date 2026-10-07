"""SQLite 存储层：账户余额 + 追加事件 + 幂等记录。

一致性设计：
- 所有多步写入都在 `transaction()` 提供的单个事务内完成
  （BEGIN IMMEDIATE 获取写锁，串行化并发写），要么全部提交、
  要么全部回滚，杜绝“事件与余额只落一边”的中间状态。
- 余额列带 CHECK (balance_cents >= 0) 约束，作为透支的最后防线。
- 所有 sqlite3 驱动异常在此层被归一化为 StorageError。
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone

from .errors import StorageError
from .models import Account, LedgerEvent

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    account_id    TEXT PRIMARY KEY,
    balance_cents INTEGER NOT NULL CHECK (balance_cents >= 0),
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    -- 作用域 + 键 共同唯一：同一标识用在不同操作/不同业务对象上各算各的，
    -- 只有同作用域同键但请求内容不同才算冲突。
    scope        TEXT NOT NULL,
    key          TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    status_code  INTEGER NOT NULL,
    response_body TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (scope, key)
);

CREATE TABLE IF NOT EXISTS events (
    event_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    transfer_id         TEXT NOT NULL,
    account_id          TEXT NOT NULL,
    kind                TEXT NOT NULL CHECK (kind IN ('debit', 'credit')),
    delta_cents         INTEGER NOT NULL,
    balance_after_cents INTEGER NOT NULL,
    idempotency_key     TEXT NOT NULL,
    reversal_of         TEXT,  -- 冲正事件指向被冲正的原始 transfer_id
    created_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_account ON events(account_id, event_id);
CREATE INDEX IF NOT EXISTS idx_events_transfer ON events(transfer_id);

-- 冲正登记：transfer_id 主键保证同一笔转账最多被冲正一次（事务内原子生效）
CREATE TABLE IF NOT EXISTS reversals (
    transfer_id     TEXT PRIMARY KEY,
    reversal_id     TEXT NOT NULL UNIQUE,
    idempotency_key TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    """线程安全的 SQLite 存储。每个事务使用独立连接。"""

    def __init__(self, db_path: str = ":memory:") -> None:
        self._db_path = db_path
        self._init_lock = threading.Lock()
        self._memory_conn: sqlite3.Connection | None = None
        if db_path == ":memory:":
            # 内存库需要共享同一个连接，否则每个连接看到不同的库
            self._memory_conn = self._new_conn()
            self._apply_schema(self._memory_conn)
        else:
            with self._init_lock:
                conn = self._new_conn()
                try:
                    self._apply_schema(conn)
                finally:
                    conn.close()

    def _new_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self._db_path,
            timeout=30.0,  # busy_timeout：写锁竞争时等待而非立即失败
            check_same_thread=False,
            isolation_level=None,  # 手动控制事务
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @staticmethod
    def _apply_schema(conn: sqlite3.Connection) -> None:
        conn.executescript(SCHEMA)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """一个 IMMEDIATE 写事务：提交或回滚，异常归一化为 StorageError。"""
        if self._memory_conn is not None:
            conn = self._memory_conn
            lock = self._init_lock  # 内存库单连接，用互斥锁串行化
            lock.acquire()
        else:
            conn = self._new_conn()
            lock = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except StorageError:
            conn.execute("ROLLBACK")
            raise
        except sqlite3.Error as exc:
            conn.execute("ROLLBACK")
            raise StorageError(f"storage operation failed: {type(exc).__name__}") from exc
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            if lock is not None:
                lock.release()
            else:
                conn.close()

    @contextmanager
    def read_conn(self) -> Iterator[sqlite3.Connection]:
        """只读连接（快照读）。"""
        if self._memory_conn is not None:
            with self._init_lock:
                yield self._memory_conn
            return
        conn = self._new_conn()
        try:
            yield conn
        except sqlite3.Error as exc:
            raise StorageError(f"storage read failed: {type(exc).__name__}") from exc
        finally:
            conn.close()

    # ---- 账户 ----

    @staticmethod
    def get_account(conn: sqlite3.Connection, account_id: str) -> Account | None:
        row = conn.execute(
            "SELECT account_id, balance_cents, created_at FROM accounts WHERE account_id = ?",
            (account_id,),
        ).fetchone()
        if row is None:
            return None
        return Account(row["account_id"], row["balance_cents"], row["created_at"])

    @staticmethod
    def insert_account(conn: sqlite3.Connection, account_id: str, balance_cents: int) -> Account:
        now = _now()
        conn.execute(
            "INSERT INTO accounts (account_id, balance_cents, created_at) VALUES (?, ?, ?)",
            (account_id, balance_cents, now),
        )
        return Account(account_id, balance_cents, now)

    @staticmethod
    def update_balance(conn: sqlite3.Connection, account_id: str, new_balance_cents: int) -> None:
        conn.execute(
            "UPDATE accounts SET balance_cents = ? WHERE account_id = ?",
            (new_balance_cents, account_id),
        )

    # ---- 事件 ----

    @staticmethod
    def insert_event(
        conn: sqlite3.Connection,
        transfer_id: str,
        account_id: str,
        kind: str,
        delta_cents: int,
        balance_after_cents: int,
        idempotency_key: str,
        reversal_of: str | None = None,
    ) -> None:
        conn.execute(
            "INSERT INTO events (transfer_id, account_id, kind, delta_cents,"
            " balance_after_cents, idempotency_key, reversal_of, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (transfer_id, account_id, kind, delta_cents, balance_after_cents,
             idempotency_key, reversal_of, _now()),
        )

    @staticmethod
    def _row_to_event(r: sqlite3.Row) -> LedgerEvent:
        return LedgerEvent(
            event_id=r["event_id"],
            transfer_id=r["transfer_id"],
            account_id=r["account_id"],
            kind=r["kind"],
            delta_cents=r["delta_cents"],
            balance_after_cents=r["balance_after_cents"],
            idempotency_key=r["idempotency_key"],
            created_at=r["created_at"],
            reversal_of=r["reversal_of"],
        )

    @staticmethod
    def list_events(conn: sqlite3.Connection, account_id: str | None = None) -> list[LedgerEvent]:
        if account_id is None:
            rows = conn.execute(
                "SELECT * FROM events ORDER BY event_id"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM events WHERE account_id = ? ORDER BY event_id",
                (account_id,),
            ).fetchall()
        return [Store._row_to_event(r) for r in rows]

    @staticmethod
    def list_events_for_transfer(
        conn: sqlite3.Connection, transfer_id: str
    ) -> list[LedgerEvent]:
        rows = conn.execute(
            "SELECT * FROM events WHERE transfer_id = ? ORDER BY event_id",
            (transfer_id,),
        ).fetchall()
        return [Store._row_to_event(r) for r in rows]

    # ---- 冲正登记 ----

    @staticmethod
    def get_reversal(conn: sqlite3.Connection, transfer_id: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT transfer_id, reversal_id, idempotency_key, created_at"
            " FROM reversals WHERE transfer_id = ?",
            (transfer_id,),
        ).fetchone()

    @staticmethod
    def insert_reversal(
        conn: sqlite3.Connection,
        transfer_id: str,
        reversal_id: str,
        idempotency_key: str,
    ) -> None:
        conn.execute(
            "INSERT INTO reversals (transfer_id, reversal_id, idempotency_key,"
            " created_at) VALUES (?, ?, ?, ?)",
            (transfer_id, reversal_id, idempotency_key, _now()),
        )

    # ---- 幂等记录 ----

    @staticmethod
    def get_idempotency(
        conn: sqlite3.Connection, scope: str, key: str
    ) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT scope, key, request_hash, status_code, response_body"
            " FROM idempotency_keys WHERE scope = ? AND key = ?",
            (scope, key),
        ).fetchone()

    @staticmethod
    def insert_idempotency(
        conn: sqlite3.Connection,
        scope: str,
        key: str,
        request_hash: str,
        status_code: int,
        response_body: str,
    ) -> None:
        conn.execute(
            "INSERT INTO idempotency_keys (scope, key, request_hash, status_code,"
            " response_body, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (scope, key, request_hash, status_code, response_body, _now()),
        )
