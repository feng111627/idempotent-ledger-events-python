"""账本业务逻辑：账户、转账、冲正、幂等、回放。

一致性保证：
- 每次转账/冲正的所有写入（余额更新 + 两条事件 + 幂等记录）发生在同一个
  SQLite 事务中，要么全部生效，要么全部回滚。
- 并发写由 BEGIN IMMEDIATE 串行化，配合余额 CHECK 约束，不会透支、
  不会丢更新；借贷双方一正一负同额记账，总余额守恒。
- 幂等：幂等记录按 (作用域, 键) 唯一。作用域 = 操作 + 业务对象，
  同一标识用在不同操作或不同对象上各算各的；只有同作用域同键且
  请求内容不同才算 IdempotencyConflict。
- 冲正：只追加新事件（reversal_of 指向原始转账），原始事件永不改写；
  reversals 表以 transfer_id 为主键，保证同一笔转账最多冲正一次。
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
    TransferAlreadyReversed,
    TransferNotFound,
)
from .models import Account, format_cents, parse_amount_to_cents
from .store import Store

# 幂等作用域：转账以键本身为业务对象；开户/冲正以账户/原转账为对象
_SCOPE_TRANSFER = "transfer"


def _account_scope(account_id: str) -> str:
    return f"account:{account_id}"


def _reversal_scope(transfer_id: str) -> str:
    return f"reversal:{transfer_id}"


def _request_hash(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class LedgerService:
    def __init__(self, store: Store) -> None:
        self._store = store

    # ---- 账户 ----

    def create_account(
        self,
        account_id: str,
        initial_balance_cents: int = 0,
        idempotency_key: str | None = None,
    ) -> tuple[Account, bool]:
        """开户。返回 (账户, 是否幂等重放)。

        携带 idempotency_key 时按作用域 account:{account_id} 幂等：
        同键同内容重试返回首次结果；同键不同内容 -> IdempotencyConflict；
        同一键用在别的账户/别的操作上互不影响。
        """
        if not account_id or not account_id.strip():
            raise ValueError("account_id must be non-empty")
        account_id = account_id.strip()

        scope = _account_scope(account_id)
        request_hash = _request_hash({
            "op": "create_account",
            "account_id": account_id,
            "initial_balance_cents": initial_balance_cents,
        })

        with self._store.transaction() as conn:
            if idempotency_key is not None:
                existing = Store.get_idempotency(conn, scope, idempotency_key)
                if existing is not None:
                    if existing["request_hash"] != request_hash:
                        raise IdempotencyConflict(
                            f"idempotency key {idempotency_key!r} was already used "
                            "with a different request"
                        )
                    body = json.loads(existing["response_body"])
                    return Account(
                        body["account_id"],
                        parse_amount_to_cents(body["balance"], allow_zero=True),
                        body["created_at"],
                    ), True

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
            if idempotency_key is not None:
                body = {
                    "account_id": account.account_id,
                    "balance": format_cents(account.balance_cents),
                    "created_at": account.created_at,
                }
                Store.insert_idempotency(
                    conn, scope, idempotency_key, request_hash, 201,
                    json.dumps(body),
                )
            return account, False

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
            existing = Store.get_idempotency(conn, _SCOPE_TRANSFER, idempotency_key)
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
                conn, _SCOPE_TRANSFER, idempotency_key, request_hash, 200,
                json.dumps(body)
            )
            return 200, body, False

    # ---- 冲正（幂等，全额退款） ----

    def reverse_transfer(
        self,
        transfer_id: str,
        idempotency_key: str,
    ) -> tuple[int, dict, bool]:
        """对一笔已成功转账做全额冲正。返回 (HTTP 状态码, 响应体, 是否幂等重放)。

        - 原始转账不存在 -> TransferNotFound (404)
        - 同一转账已被冲正（换了提交标识再次冲正）-> TransferAlreadyReversed (409)
        - 同键重试 -> 返回首次保存的结果，不重复记账
        - 只追加冲正事件（reversal_of 指向原转账），原始事件不改写
        """
        scope = _reversal_scope(transfer_id)
        request_hash = _request_hash({"op": "reversal", "transfer_id": transfer_id})

        with self._store.transaction() as conn:
            # 1) 幂等检查（同键重试直接重放首次结果）
            existing = Store.get_idempotency(conn, scope, idempotency_key)
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

            # 2) 找到原始转账的借贷事件对
            originals = [
                e for e in Store.list_events_for_transfer(conn, transfer_id)
                if e.reversal_of is None
            ]
            debit = next((e for e in originals if e.kind == "debit"), None)
            credit = next((e for e in originals if e.kind == "credit"), None)
            if debit is None or credit is None:
                raise TransferNotFound(
                    f"transfer {transfer_id!r} not found or not reversible"
                )

            # 3) 同一笔转账最多冲正一次（reversals 表主键兜底）
            if Store.get_reversal(conn, transfer_id) is not None:
                raise TransferAlreadyReversed(
                    f"transfer {transfer_id!r} has already been reversed"
                )

            # 4) 全额退款：钱退回原付款方
            from_account = debit.account_id   # 原付款方
            to_account = credit.account_id    # 原收款方
            amount_cents = credit.delta_cents
            payer = Store.get_account(conn, from_account)
            payee = Store.get_account(conn, to_account)
            if payer is None:
                raise AccountNotFound(f"account {from_account!r} not found")
            if payee is None:
                raise AccountNotFound(f"account {to_account!r} not found")
            if payee.balance_cents < amount_cents:
                raise InsufficientFunds(
                    f"account {to_account!r} balance "
                    f"{format_cents(payee.balance_cents)} cannot cover reversal "
                    f"of {format_cents(amount_cents)}"
                )

            reversal_id = uuid.uuid4().hex
            payee_after = payee.balance_cents - amount_cents
            payer_after = payer.balance_cents + amount_cents
            Store.update_balance(conn, to_account, payee_after)
            Store.update_balance(conn, from_account, payer_after)
            Store.insert_event(
                conn, reversal_id, to_account, "debit",
                -amount_cents, payee_after, idempotency_key,
                reversal_of=transfer_id,
            )
            Store.insert_event(
                conn, reversal_id, from_account, "credit",
                amount_cents, payer_after, idempotency_key,
                reversal_of=transfer_id,
            )
            Store.insert_reversal(conn, transfer_id, reversal_id, idempotency_key)

            body = {
                "reversal_id": reversal_id,
                "transfer_id": transfer_id,
                "from_account": from_account,
                "to_account": to_account,
                "amount": format_cents(amount_cents),
                "from_balance": format_cents(payer_after),
                "to_balance": format_cents(payee_after),
            }
            Store.insert_idempotency(
                conn, scope, idempotency_key, request_hash, 200, json.dumps(body)
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

        # 冲正一览：从事件流中还原“原转账 -> 冲正”的对应关系
        reversals: dict[str, dict] = {}
        for ev in events:
            if ev.reversal_of is None:
                continue
            entry = reversals.setdefault(ev.reversal_of, {
                "transfer_id": ev.reversal_of,
                "reversal_id": ev.transfer_id,
                "amount_cents": 0,
            })
            if ev.kind == "credit":
                entry["amount_cents"] += ev.delta_cents
        report_reversals = []
        for tid in sorted(reversals):
            r = reversals[tid]
            amount_cents = r.pop("amount_cents")
            report_reversals.append({**r, "amount": format_cents(amount_cents)})

        return {
            "consistent": consistent,
            "event_count": len(events),
            "accounts": report_accounts,
            "reversals": report_reversals,
            "chain_errors": chain_errors,
        }
