"""仓储层：所有 SQL 操作集中在此，底层异常统一归一化。

关键一致性手段：
- 每次写操作在单个数据库事务内完成「追加事件 + 更新余额」，
  二者要么同时落库要么同时回滚，不会只落一边；
- 写事务以 BEGIN IMMEDIATE 获取 RESERVED 锁，串行化写者，
  杜绝并发转账的丢更新；
- 余额更新为条件更新（要求新余额非负），数据库层兜底防透支。
"""
from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from app.domain import Account, Amount, EventType, LedgerEvent


class LedgerRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # ---- 读 ----
    def get_account(self, account_id: str) -> Account | None:
        raise NotImplementedError

    def list_events(self, account_id: str | None = None) -> list[LedgerEvent]:
        raise NotImplementedError

    def get_idempotent_record(self, key: str) -> tuple[str, str, str] | None:
        """返回 (operation, request_hash, response_json)，不存在返回 None。"""
        raise NotImplementedError

    # ---- 写 ----
    def insert_account(self, account_id: str, opening: Amount, now_iso: str,
                       response_json: str, request_hash: str,
                       idem_key: str | None) -> Account:
        raise NotImplementedError

    def transfer(self, transfer_id: str, from_id: str, to_id: str,
                 amount: Amount, now_iso: str, response_json: str,
                 request_hash: str) -> None:
        """原子地完成：条件扣款 + 入账 + 两条事件 + 幂等记录。"""
        raise NotImplementedError
