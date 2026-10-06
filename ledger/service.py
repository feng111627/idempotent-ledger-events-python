"""账本业务逻辑：账户、转账、幂等、回放。

一致性保证：
- 每次转账的所有写入（余额更新 + 两条事件 + 幂等记录）发生在同一个
  SQLite 事务中，要么全部生效，要么全部回滚。
- 并发写由 BEGIN IMMEDIATE 串行化，配合余额 CHECK 约束，不会透支、
  不会丢更新；借贷双方一正一负同额记账，总余额守恒。
- 幂等：同一幂等键 + 相同请求内容 -> 返回首次保存的响应，不重复记账；
  同一幂等键 + 不同请求内容 -> IdempotencyConflict。
"""

from __future__ import annotations

import hashlib
import json
import uuid

from .errors import (
    AccountAlreadyExists,
    AccountNotFound,
    IdempotencyConflict,
    InsufficientFunds,
    SameAccountTransfer,
)
from .models import Account, format_cents
from .store import Store


def _request_hash(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class LedgerService:
    def __init__(self, store: Store) -> None:
        self._store = store

    # ---- 账户 ----

    def create_account(self, account_id: str, initial_balance_cents: int = 0) -> Account:
        if not account_id or not account_id.strip():
            raise ValueError("account_id must be non-empty")
        account_id = account_id.strip()
        with self._store.transaction() as conn:
            if Store.get_account(conn, account_id) is not None:
                raise AccountAlreadyExists(f"account {account_id!r} already exists")
            account = Store.insert_account(conn, account_id, initial_balance_cents)
            if initial_balance_cents > 0:
                # 初始余额也记为一条事件，保证回放可重建
                Store.insert_event(
                    conn,
                    transfer_id=f"init:{account_id}",
                    account_id=account_id,
                    kind="credit",
                    delta_cents=initial_balance_cents,
                    balance_after_cents=initial_balance_cents,
                    idempotency_key=f"init:{account_id}",
                )
            return account

    def get_account(self, account_id: str) -> Account:
        with self._store.read_conn() as conn:
            account = Store.get_account(conn, account_id)
        if account is None:
            raise AccountNotFound(f"account {account_id!r} not found")
        return account

    # ---- 转账（幂等） ----

    def transfer(
        self,
        from_account: str,
        to_account: str,
        amount_cents: int,
        idempotency_key: str,
    ) -> tuple[int, dict, bool]:
        """执行转账。返回 (HTTP 状态码, 响应体, 是否幂等重放)。"""
        if from_account == to_account:
            raise SameAccountTransfer("from_account and to_account must differ")

        request_hash = _request_hash({
            "op": "transfer",
            "from": from_account,
            "to": to_account,
            "amount_cents": amount_cents,
        })

        with self._store.transaction() as conn:
            # 1) 幂等检查
            existing = Store.get_idempotency(conn, idempotency_key)
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise IdempotencyConflict(
                        f"idempotency key {idempotency_key!r} was already used "
                        "with a different request"
                    )
                return (
                    existing["status_code"],
                    json.loads(existing["response_body"]),
                    True,
                )

            # 2) 业务校验与记账（同事务）
            src = Store.get_account(conn, from_account)
            if src is None:
                raise AccountNotFound(f"account {from_account!r} not found")
            dst = Store.get_account(conn, to_account)
            if dst is None:
                raise AccountNotFound(f"account {to_account!r} not found")
            if src.balance_cents < amount_cents:
                raise InsufficientFunds(
                    f"account {from_account!r} balance "
                    f"{format_cents(src.balance_cents)} < amount "
                    f"{format_cents(amount_cents)}"
                )

            transfer_id = uuid.uuid4().hex
            src_after = src.balance_cents - amount_cents
            dst_after = dst.balance_cents + amount_cents
            Store.update_balance(conn, from_account, src_after)
            Store.update_balance(conn, to_account, dst_after)
            Store.insert_event(
                conn, transfer_id, from_account, "debit",
                -amount_cents, src_after, idempotency_key,
            )
            Store.insert_event(
                conn, transfer_id, to_account, "credit",
                amount_cents, dst_after, idempotency_key,
            )

            body = {
                "transfer_id": transfer_id,
                "from_account": from_account,
                "to_account": to_account,
                "amount": format_cents(amount_cents),
                "from_balance": format_cents(src_after),
                "to_balance": format_cents(dst_after),
            }
            Store.insert_idempotency(
                conn, idempotency_key, request_hash, 200, json.dumps(body)
            )
            return 200, body, False

    # ---- 回放 ----

    def replay(self, account_id: str | None = None) -> dict:
        """按事件流重放，重建余额并与当前余额比对。

        同时校验每条事件的 balance_after 是否与重放的运行余额一致
        （事件链完整性）。
        """
        with self._store.read_conn() as conn:
            events = Store.list_events(conn, account_id)
            if account_id is not None:
                account = Store.get_account(conn, account_id)
                if account is None:
                    raise AccountNotFound(f"account {account_id!r} not found")
                accounts = {account_id: account}
            else:
                rows = conn.execute(
                    "SELECT account_id, balance_cents, created_at FROM accounts"
                ).fetchall()
                accounts = {
                    r["account_id"]: Account(r["account_id"], r["balance_cents"],
                                             r["created_at"])
                    for r in rows
                }

        running: dict[str, int] = {aid: 0 for aid in accounts}
        chain_errors: list[dict] = []
        for ev in events:
            before = running.get(ev.account_id, 0)
            after = before + ev.delta_cents
            if ev.balance_after_cents != after:
                chain_errors.append({
                    "event_id": ev.event_id,
                    "account_id": ev.account_id,
                    "expected_balance_after": after,
                    "recorded_balance_after": ev.balance_after_cents,
                })
            running[ev.account_id] = after

        report_accounts = []
        consistent = not chain_errors
        for aid, account in sorted(accounts.items()):
            rebuilt = running.get(aid, 0)
            match = rebuilt == account.balance_cents
            consistent = consistent and match
            report_accounts.append({
                "account_id": aid,
                "stored_balance": format_cents(account.balance_cents),
                "rebuilt_balance": format_cents(rebuilt),
                "match": match,
            })

        return {
            "consistent": consistent,
            "event_count": len(events),
            "accounts": report_accounts,
            "chain_errors": chain_errors,
        }
