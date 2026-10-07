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
from .models import Account, LedgerEvent, Reversal, TransferRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    account_id    TEXT PRIMARY KEY,
    balance_cents INTEGER NOT NULL CHECK (balance_cents >= 0),
    created_at    TEXT NOT NULL
);

-- 幂等记录按“操作 + 业务对象”作用域存储：
-- 同一个 key 用在不同操作或不同对象上各算各的，互不覆盖、互不冲突；
-- 同一 (operation, scope_type, scope_id, key) 内，request_hash 不同才算真冲突。
CREATE TABLE IF NOT EXISTS idempotency_keys (
    operation     TEXT NOT NULL,
    scope_type    TEXT NOT NULL,
    scope_id      TEXT NOT NULL,
    key           TEXT NOT NULL,
    request_hash  TEXT NOT NULL,
    status_code   INTEGER NOT NULL,
    response_body TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    PRIMARY KEY (operation, scope_type, scope_id, key)
);

CREATE TABLE IF NOT EXISTS events (
    event_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    transfer_id         TEXT NOT NULL,
    account_id          TEXT NOT NULL,
    kind                TEXT NOT NULL CHECK (kind IN
                        ('debit', 'credit', 'reversal_debit', 'reversal_credit')),
    delta_cents         INTEGER NOT NULL,
    balance_after_cents INTEGER NOT NULL,
    idempotency_key     TEXT NOT NULL,
    created_at          TEXT NOT NULL
);

-- 每笔转账最多冲正一次：transfer_id 唯一约束与 BEGIN IMMEDIATE
-- 共同保证并发冲正只有一次成功（冲正事件同样追加写入，原转账事件不改不删）。
CREATE TABLE IF NOT EXISTS reversals (
    reversal_id     TEXT PRIMARY KEY,
    transfer_id     TEXT NOT NULL UNIQUE,
    account_id      TEXT NOT NULL,
    amount_cents    INTEGER NOT NULL,
    idempotency_key TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_account ON events(account_id, event_id);
CREATE INDEX IF NOT EXISTS idx_events_transfer ON events(transfer_id, event_id);
CREATE INDEX IF NOT EXISTS idx_reversals_transfer ON reversals(transfer_id);
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
    ) -> None:
        conn.execute(
            "INSERT INTO events (transfer_id, account_id, kind, delta_cents,"
            " balance_after_cents, idempotency_key, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (transfer_id, account_id, kind, delta_cents, balance_after_cents,
             idempotency_key, _now()),
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
        return [
            LedgerEvent(
                event_id=r["event_id"],
                transfer_id=r["transfer_id"],
                account_id=r["account_id"],
                kind=r["kind"],
                delta_cents=r["delta_cents"],
                balance_after_cents=r["balance_after_cents"],
                idempotency_key=r["idempotency_key"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    # ---- 幂等记录（按 操作 + 业务对象 作用域）----

    @staticmethod
    def get_idempotency(
        conn: sqlite3.Connection,
        key: str,
        operation: str,
        scope_type: str,
        scope_id: str,
    ) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT key, request_hash, status_code, response_body FROM idempotency_keys"
            " WHERE operation = ? AND scope_type = ? AND scope_id = ? AND key = ?",
            (operation, scope_type, scope_id, key),
        ).fetchone()

    @staticmethod
    def insert_idempotency(
        conn: sqlite3.Connection,
        key: str,
        operation: str,
        scope_type: str,
        scope_id: str,
        request_hash: str,
        status_code: int,
        response_body: str,
    ) -> None:
        conn.execute(
            "INSERT INTO idempotency_keys (operation, scope_type, scope_id, key,"
            " request_hash, status_code, response_body, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (operation, scope_type, scope_id, key, request_hash, status_code,
             response_body, _now()),
        )

    # ---- 转账聚合（从追加事件读取，不改历史）----

    @staticmethod
    def get_transfer(
        conn: sqlite3.Connection, transfer_id: str
    ) -> TransferRecord | None:
        """按 transfer_id 聚合出一笔成功的原始转账；不存在返回 None。

        只认 kind='debit' 的原始付款事件，冲正事件（reversal_*）不参与，
        因此冲正后仍能还原出原始转账。
        """
        row = conn.execute(
            "SELECT transfer_id, account_id AS from_account, delta_cents"
            " FROM events WHERE transfer_id = ? AND kind = 'debit'",
            (transfer_id,),
        ).fetchone()
        if row is None:
            return None
        credit = conn.execute(
            "SELECT account_id FROM events WHERE transfer_id = ? AND kind = 'credit'",
            (transfer_id,),
        ).fetchone()
        if credit is None:
            return None
        return TransferRecord(
            transfer_id=row["transfer_id"],
            from_account=row["from_account"],
            to_account=credit["account_id"],
            amount_cents=-row["delta_cents"],
        )

    # ---- 冲正（追加记录）----

    @staticmethod
    def get_reversal_by_transfer(
        conn: sqlite3.Connection, transfer_id: str
    ) -> Reversal | None:
        row = conn.execute(
            "SELECT reversal_id, transfer_id, account_id, amount_cents,"
            " idempotency_key, created_at FROM reversals WHERE transfer_id = ?",
            (transfer_id,),
        ).fetchone()
        if row is None:
            return None
        return Reversal(
            reversal_id=row["reversal_id"],
            transfer_id=row["transfer_id"],
            account_id=row["account_id"],
            amount_cents=row["amount_cents"],
            idempotency_key=row["idempotency_key"],
            created_at=row["created_at"],
        )

    @staticmethod
    def list_reversals(conn: sqlite3.Connection) -> list[Reversal]:
        rows = conn.execute(
            "SELECT reversal_id, transfer_id, account_id, amount_cents,"
            " idempotency_key, created_at FROM reversals ORDER BY rowid"
        ).fetchall()
        return [
            Reversal(
                reversal_id=r["reversal_id"],
                transfer_id=r["transfer_id"],
                account_id=r["account_id"],
                amount_cents=r["amount_cents"],
                idempotency_key=r["idempotency_key"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    @staticmethod
    def insert_reversal(
        conn: sqlite3.Connection,
        reversal_id: str,
        transfer_id: str,
        account_id: str,
        amount_cents: int,
        idempotency_key: str,
    ) -> None:
        """写入冲正记录。transfer_id 唯一约束在并发下拒绝第二次冲正，
        驱动 IntegrityError 由事务包装归一化为 StorageError，由 service 层
        在此之前的同事务复查区分出 TransferAlreadyReversed。
        """
        conn.execute(
            "INSERT INTO reversals (reversal_id, transfer_id, account_id,"
            " amount_cents, idempotency_key, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (reversal_id, transfer_id, account_id, amount_cents,
             idempotency_key, _now()),
        )
