"""仓储层：所有 SQL 操作集中在此，底层异常统一归一化。

关键一致性手段：
- 每个写操作在单个数据库事务内完成「追加事件 + 更新余额」，
  二者要么同时成功要么一起回滚，不会只落一边；
- 写事务以 BEGIN IMMEDIATE 获取 RESERVED 锁，写者之间串行，
  杜绝并发转账的丢更新；
- 余额更新为条件更新（要求当前余额 >= 转出额），数据库层兜底防透支；
- 每条事件带全局单调递增 seq 与应用后余额，回放即权威账户状态。
"""
from __future__ import annotations

import logging
import sqlite3

from app.domain import Account, Amount, EventType, LedgerEvent
from app.errors import (
    AccountAlreadyExists,
    AccountNotFound,
    IdempotencyConflict,
    InsufficientFunds,
    StorageUnavailable,
)

log = logging.getLogger("ledger.repo")

# 驱动层错误码 -> 归一化错误
_INTEGRITY_ERRORS = {
    "SQLITE_CONSTRAINT_PRIMARYKEY",
    "SQLITE_CONSTRAINT_UNIQUE",
    "SQLITE_CONSTRAINT_FOREIGNKEY",
    "SQLITE_CONSTRAINT_CHECK",
}


def _wrap_db_error(exc: sqlite3.Error) -> StorageUnavailable:
    """把驱动异常包成归一化错误（保留内部日志，不透出原始信息给调用方）。"""
    log.warning("底层数据库异常已归一化: %s", type(exc).__name__)
    return StorageUnavailable("存储暂时不可用，请稍后重试")


class LedgerRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # ------------------------------------------------------------------ 读
    def get_account(self, account_id: str) -> Account | None:
        try:
            row = self._conn.execute(
                "SELECT account_id, balance FROM accounts WHERE account_id = ?",
                (account_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise _wrap_db_error(exc) from None
        if row is None:
            return None
        return Account(account_id=row["account_id"], balance=Amount(row["balance"]))

    def list_events(self, account_id: str | None = None) -> list[LedgerEvent]:
        try:
            if account_id is None:
                rows = self._conn.execute(
                    "SELECT seq, account_id, event_type, amount, balance_after,"
                    " transfer_id, timestamp FROM events ORDER BY seq"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT seq, account_id, event_type, amount, balance_after,"
                    " transfer_id, timestamp FROM events"
                    " WHERE account_id = ? ORDER BY seq",
                    (account_id,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise _wrap_db_error(exc) from None
        return [self._row_to_event(r) for r in rows]

    def get_idempotent_record(self, key: str) -> tuple[str, str, str] | None:
        """返回 (operation, request_hash, response_json)，不存在返回 None。"""
        try:
            row = self._conn.execute(
                "SELECT operation, request_hash, response_json"
                " FROM idempotency WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise _wrap_db_error(exc) from None
        if row is None:
            return None
        return row["operation"], row["request_hash"], row["response_json"]

    # ------------------------------------------------------------------ 写
    def insert_account(self, account_id: str, opening: Amount, now_iso: str,
                       response_json: str, request_hash: str,
                       idem_key: str | None) -> Account:
        """单事务：建账户 + OPEN 事件（+ 可选幂等记录）。"""
        conn = self._conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            if idem_key is not None:
                self._insert_idempotency(
                    conn, idem_key, "create_account", request_hash,
                    response_json, now_iso)
            conn.execute(
                "INSERT INTO accounts(account_id, balance, created_at)"
                " VALUES (?, ?, ?)",
                (account_id, opening, now_iso),
            )
            seq = self._insert_event(
                conn, account_id, EventType.OPEN, opening, opening, None, now_iso)
            conn.execute("COMMIT")
        except (AccountAlreadyExists, IdempotencyConflict):
            conn.execute("ROLLBACK")
            raise
        except sqlite3.IntegrityError as exc:
            conn.execute("ROLLBACK")
            # accounts 主键冲突 -> 账户已存在；幂等键冲突单独区分
            if _is_pk_conflict(exc, on="accounts"):
                raise AccountAlreadyExists(
                    f"账户 {account_id!r} 已存在") from None
            raise _wrap_db_error(exc) from None
        except sqlite3.Error as exc:
            conn.execute("ROLLBACK")
            raise _wrap_db_error(exc) from None

        log.info("开户成功 account=%s opening=%s seq=%s",
                 account_id, opening, seq)
        return Account(account_id=account_id, balance=Amount(opening))

    def transfer(self, transfer_id: str, from_id: str, to_id: str,
                 amount: Amount, now_iso: str, response_json: str,
                 request_hash: str) -> None:
        """原子完成：幂等记录 + 条件扣款 + 入账 + 两条事件。

        必须保证 from 与 to 都存在、from 与 to 不同、余额充足。
        任意一步失败整体回滚，事件与余额不会只落一边。
        """
        conn = self._conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._insert_idempotency(
                conn, transfer_id, "transfer", request_hash,
                response_json, now_iso)

            src = conn.execute(
                "SELECT balance FROM accounts WHERE account_id = ?",
                (from_id,),
            ).fetchone()
            if src is None:
                raise AccountNotFound(f"转出账户 {from_id!r} 不存在")
            if src["balance"] < amount:
                raise InsufficientFunds(
                    f"账户 {from_id!r} 余额不足："
                    f"余额 {src['balance']}，申请转出 {amount}")

            dst = conn.execute(
                "SELECT balance FROM accounts WHERE account_id = ?",
                (to_id,),
            ).fetchone()
            if dst is None:
                raise AccountNotFound(f"转入账户 {from_id!r} 不存在")

            src_after = src["balance"] - amount
            dst_after = dst["balance"] + amount
            # 条件更新：数据库层再次兜底，杜绝并发下透支/丢更新
            cur = conn.execute(
                "UPDATE accounts SET balance = ?"
                " WHERE account_id = ? AND balance >= ?",
                (src_after, from_id, amount),
            )
            if cur.rowcount != 1:
                raise InsufficientFunds(
                    f"账户 {from_id!r} 并发扣款冲突，余额不足")
            conn.execute(
                "UPDATE accounts SET balance = ? WHERE account_id = ?",
                (dst_after, to_id),
            )
            seq_out = self._insert_event(
                conn, from_id, EventType.TRANSFER_OUT, amount,
                Amount(src_after), transfer_id, now_iso)
            seq_in = self._insert_event(
                conn, to_id, EventType.TRANSFER_IN, amount,
                Amount(dst_after), transfer_id, now_iso)
            conn.execute("COMMIT")
        except (AccountNotFound, InsufficientFunds, IdempotencyConflict):
            conn.execute("ROLLBACK")
            raise
        except sqlite3.Error as exc:
            conn.execute("ROLLBACK")
            raise _wrap_db_error(exc) from None

        log.info("转账成功 idem=%s %s->%s amount=%s seqs=%s,%s",
                 transfer_id, from_id, to_id, amount, seq_out, seq_in)

    # -------------------------------------------------------------- helpers
    @staticmethod
    def _insert_idempotency(conn: sqlite3.Connection, key: str, operation: str,
                            request_hash: str, response_json: str,
                            now_iso: str) -> None:
        """幂等键冲突时区分「重放」与「同键换内容」由 service 层处理，
        这里直接插入；若已存在则抛唯一约束（不静默覆盖）。"""
        conn.execute(
            "INSERT INTO idempotency(idempotency_key, operation, request_hash,"
            " response_json, status, created_at)"
            " VALUES (?, ?, ?, ?, 'done', ?)",
            (key, operation, request_hash, response_json, now_iso),
        )

    @staticmethod
    def _insert_event(conn: sqlite3.Connection, account_id: str,
                      event_type: EventType, amount: Amount,
                      balance_after: Amount, transfer_id: str | None,
                      timestamp: str) -> int:
        cur = conn.execute(
            "INSERT INTO events(account_id, event_type, amount, balance_after,"
            " transfer_id, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
            (account_id, event_type.value, amount, balance_after,
             transfer_id, timestamp),
        )
        return int(cur.lastrowid)

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> LedgerEvent:
        return LedgerEvent(
            seq=row["seq"],
            account_id=row["account_id"],
            event_type=EventType(row["event_type"]),
            amount=Amount(row["amount"]),
            balance_after=Amount(row["balance_after"]),
            transfer_id=row["transfer_id"],
            timestamp=row["timestamp"],
        )


def _is_pk_conflict(exc: sqlite3.IntegrityError, on: str) -> bool:
    """粗粒度判断是否为某表主键冲突（驱动错误信息不纳入对外响应）。"""
    return on in ("accounts",) and exc.sqlite_errorname in (
        "SQLITE_CONSTRAINT_PRIMARYKEY",
    )
