"""账本业务逻辑：账户、转账、冲正、幂等、回放。

一致性保证：
- 每次转账 / 冲正的所有写入（余额更新 + 两条事件 + 幂等记录，
  冲正另有一条 reversals 记录）发生在同一个 SQLite 事务中，
  要么全部生效，要么全部回滚。
- 并发写由 BEGIN IMMEDIATE 串行化，配合余额 CHECK 约束，不会透支、
  不会丢更新；借贷双方一正一负同额记账（冲正同样如此），总余额守恒。
- 冲正纯追加：原转账事件不改不删；一笔转账最多冲正一次
  （前置复查 + reversals.transfer_id 唯一约束双保险）。
- 幂等按“操作 + 业务对象”作用域：同作用域同键同内容 -> 返回首次保存的
  响应，不重复记账；同作用域同键不同内容 -> IdempotencyConflict；
  同键用于不同操作或不同对象各算各的。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
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
from .models import Account, format_cents
from .store import Store

# 幂等键的作用域维度：同一个 key 只有在同一操作、同一业务对象下才共享记录。
OP_CREATE_ACCOUNT = "create_account"
OP_TRANSFER = "transfer"
OP_REVERSE_TRANSFER = "reverse_transfer"
SCOPE_ACCOUNT = "account"
SCOPE_TRANSFER_PAIR = "transfer_pair"
SCOPE_TRANSFER = "transfer"


def _request_hash(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _transfer_scope_id(from_account: str, to_account: str) -> str:
    return f"{from_account}->{to_account}"


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

        幂等作用域为账户本身：同一 key 对同一 account_id + 相同初始金额
        重复提交返回首次结果；同 key 用于别的账户互不影响；
        同 key 同账户但初始金额不同 -> IdempotencyConflict。
        """
        if not account_id or not account_id.strip():
            raise ValueError("account_id must be non-empty")
        account_id = account_id.strip()

        if idempotency_key is None:
            # 未携带提交标识：保持原有“重复开户即冲突”的行为
            with self._store.transaction() as conn:
                if Store.get_account(conn, account_id) is not None:
                    raise AccountAlreadyExists(
                        f"account {account_id!r} already exists"
                    )
                return self._insert_new_account(
                    conn, account_id, initial_balance_cents
                ), False

        request_hash = _request_hash({
            "op": OP_CREATE_ACCOUNT,
            "account_id": account_id,
            "initial_balance_cents": initial_balance_cents,
        })
        with self._store.transaction() as conn:
            existing = Store.get_idempotency(
                conn, idempotency_key, OP_CREATE_ACCOUNT,
                SCOPE_ACCOUNT, account_id,
            )
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise IdempotencyConflict(
                        f"idempotency key {idempotency_key!r} was already used "
                        "with a different request"
                    )
                body = json.loads(existing["response_body"])
                return Account(
                    account_id=body["account_id"],
                    balance_cents=initial_balance_cents,
                    created_at=body["created_at"],
                ), True

            if Store.get_account(conn, account_id) is not None:
                raise AccountAlreadyExists(
                    f"account {account_id!r} already exists"
                )
            account = self._insert_new_account(
                conn, account_id, initial_balance_cents
            )
            body = {
                "account_id": account.account_id,
                "created_at": account.created_at,
            }
            Store.insert_idempotency(
                conn, idempotency_key, OP_CREATE_ACCOUNT,
                SCOPE_ACCOUNT, account_id,
                request_hash, 201, json.dumps(body),
            )
            return account, False

    @staticmethod
    def _insert_new_account(
        conn, account_id: str, initial_balance_cents: int
    ) -> Account:
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

        scope_type = SCOPE_TRANSFER_PAIR
        scope_id = _transfer_scope_id(from_account, to_account)
        request_hash = _request_hash({
            "op": OP_TRANSFER,
            "from": from_account,
            "to": to_account,
            "amount_cents": amount_cents,
        })

        with self._store.transaction() as conn:
            # 1) 幂等检查（作用域：同一付款方->收款方的转账）
            existing = Store.get_idempotency(
                conn, idempotency_key, OP_TRANSFER, scope_type, scope_id
            )
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
                conn, idempotency_key, OP_TRANSFER, scope_type, scope_id,
                request_hash, 200, json.dumps(body),
            )
            return 200, body, False

    # ---- 冲正 / 退款（幂等，追加记录）----

    def reverse_transfer(
        self, transfer_id: str, idempotency_key: str
    ) -> tuple[int, dict, bool]:
        """对一笔已成功的转账做一次全额冲正，钱退回原付款方。

        - 原转账事件不改不删，冲正以两条追加事件 + 一条 reversals 记录落库。
        - 每笔转账最多冲正一次：不存在的转账 -> TransferNotFound；
          已冲正 -> TransferAlreadyReversed，两者错误原因可区分。
        - 幂等：同 key + 同目标转账 -> 首次结果；同 key 用于别的转账
          各算各的；同 key 同转账不允许别的请求内容。
        """
        scope_type = SCOPE_TRANSFER
        scope_id = transfer_id
        request_hash = _request_hash({
            "op": OP_REVERSE_TRANSFER,
            "transfer_id": transfer_id,
        })

        with self._store.transaction() as conn:
            # 1) 幂等检查（作用域：被冲正的那笔转账）
            existing = Store.get_idempotency(
                conn, idempotency_key, OP_REVERSE_TRANSFER,
                scope_type, scope_id,
            )
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

            # 2) 原始转账必须存在（只认原始 debit 事件，冲正事件不影响）
            transfer = Store.get_transfer(conn, transfer_id)
            if transfer is None:
                raise TransferNotFound(
                    f"transfer {transfer_id!r} not found"
                )

            # 3) 同一笔转账最多冲正一次（事务串行化 + 唯一约束双保险）
            prior_reversal = Store.get_reversal_by_transfer(conn, transfer_id)
            if prior_reversal is not None:
                raise TransferAlreadyReversed(
                    f"transfer {transfer_id!r} has already been reversed"
                )

            payer_id = transfer.from_account
            payee_id = transfer.to_account
            amount_cents = transfer.amount_cents

            payer = Store.get_account(conn, payer_id)
            payee = Store.get_account(conn, payee_id)
            if payee is None or payer is None:
                raise AccountNotFound("account involved in transfer not found")
            if payee.balance_cents < amount_cents:
                # 收款方当前余额不足以退回：不允许把任何账户冲成负数
                raise InsufficientFunds(
                    f"account {payee_id!r} balance "
                    f"{format_cents(payee.balance_cents)} < reversal amount "
                    f"{format_cents(amount_cents)}"
                )

            payee_after = payee.balance_cents - amount_cents
            payer_after = payer.balance_cents + amount_cents
            reversal_id = uuid.uuid4().hex

            # 追加事件：原转账历史完全不动
            Store.update_balance(conn, payee_id, payee_after)
            Store.update_balance(conn, payer_id, payer_after)
            Store.insert_event(
                conn, transfer_id, payee_id, "reversal_debit",
                -amount_cents, payee_after, idempotency_key,
            )
            Store.insert_event(
                conn, transfer_id, payer_id, "reversal_credit",
                amount_cents, payer_after, idempotency_key,
            )
            # reversals.transfer_id UNIQUE：并发下第二个冲正落库即失败。
            # 正常路径下前置复查已挡住；唯一约束是第二道防线，命中时同样
            # 归一化为“已冲正”业务错误（而不是暴露成存储异常）。
            try:
                Store.insert_reversal(
                    conn, reversal_id, transfer_id, payer_id,
                    amount_cents, idempotency_key,
                )
            except sqlite3.IntegrityError:
                raise TransferAlreadyReversed(
                    f"transfer {transfer_id!r} has already been reversed"
                ) from None

            body = {
                "reversal_id": reversal_id,
                "transfer_id": transfer_id,
                "refund_to_account": payer_id,
                "debit_account": payee_id,
                "amount": format_cents(amount_cents),
                "refund_to_balance": format_cents(payer_after),
                "debit_balance": format_cents(payee_after),
            }
            Store.insert_idempotency(
                conn, idempotency_key, OP_REVERSE_TRANSFER,
                scope_type, scope_id,
                request_hash, 200, json.dumps(body),
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
            all_reversals = Store.list_reversals(conn)
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

        transfer_summaries = self._summarize_transfers(events)
        reversed_ids = {r.transfer_id for r in all_reversals}
        # 单账户回放时，只汇总本账户事件中出现过的转账对应的冲正
        visible_ids = {t["transfer_id"] for t in transfer_summaries}
        reversal_summaries = [
            {
                "reversal_id": r.reversal_id,
                "transfer_id": r.transfer_id,
                "refund_to_account": r.account_id,
                "amount": format_cents(r.amount_cents),
                "idempotency_key": r.idempotency_key,
            }
            for r in all_reversals if r.transfer_id in visible_ids
        ]
        for t in transfer_summaries:
            t["reversed"] = t["transfer_id"] in reversed_ids

        return {
            "consistent": consistent,
            "event_count": len(events),
            "accounts": report_accounts,
            "transfers": transfer_summaries,
            "reversals": reversal_summaries,
            "chain_errors": chain_errors,
        }

    @staticmethod
    def _summarize_transfers(events: list) -> list[dict]:
        """从事件流聚合出每笔转账及其冲正情况（原转账与冲正都可见）。"""
        groups: dict[str, dict] = {}
        order: list[str] = []
        for ev in events:
            if ev.kind not in ("debit", "credit",
                               "reversal_debit", "reversal_credit"):
                continue
            g = groups.get(ev.transfer_id)
            if g is None:
                g = groups[ev.transfer_id] = {"_first": ev.event_id}
                order.append(ev.transfer_id)
            if ev.kind == "debit":
                g["transfer_id"] = ev.transfer_id
                g["from_account"] = ev.account_id
                g["amount"] = format_cents(-ev.delta_cents)
            elif ev.kind == "credit":
                g.setdefault("transfer_id", ev.transfer_id)
                g["to_account"] = ev.account_id
        result = []
        for tid in order:
            g = groups[tid]
            if "from_account" not in g:
                # 只有 credit 的是开户初始入金（init:*），不是转账
                continue
            result.append({
                "transfer_id": g.get("transfer_id", tid),
                "from_account": g["from_account"],
                "to_account": g.get("to_account"),
                "amount": g.get("amount"),
                "reversed": False,
            })
        return result
