"""业务服务层：账户、转账、幂等与事件回放。"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime

from app.domain import LedgerEvent
from app.errors import (
    AccountNotFound,
    IdempotentReplay,
    IdempotencyConflict,
    InvalidAmount,
    ReplayMismatch,
)
from app.repository import LedgerRepository

log = logging.getLogger("ledger.service")


def _now_iso() -> str:
    """UTC ISO-8601 时间戳，保证回放/排序跨环境可复现。"""
    return datetime.now(UTC).isoformat(timespec="microseconds")


def canonical_request_hash(payload: dict) -> str:
    """对请求内容做规范化哈希，用于识别「同 key 换内容」。"""
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def money_str(minor: int) -> str:
    """整数最小单位 -> 两位十进制字符串（仅用于展示，不参与运算）。"""
    sign = "-" if minor < 0 else ""
    v = abs(int(minor))
    return f"{sign}{v // 100}.{v % 100:02d}"


class LedgerService:
    def __init__(self, repo: LedgerRepository) -> None:
        self._repo = repo

    # ------------------------------------------------------------ 开户
    def create_account(self, account_id: str, opening_minor: int,
                       idem_key: str | None = None) -> dict:
        if not account_id:
            raise InvalidAmount("账户标识不能为空")
        if opening_minor < 0:
            raise InvalidAmount("开户金额不能为负")
        req = {"account_id": account_id, "opening_minor": opening_minor}
        preview = {
            "account_id": account_id,
            "balance": money_str(opening_minor),
            "balance_minor": opening_minor,
            "replayed": False,
        }

        def write(resp_json: str) -> dict:
            self._repo.insert_account(
                account_id, opening_minor, _now_iso(),
                resp_json, canonical_request_hash(req), idem_key)
            return preview

        return self._run("create_account", idem_key, req, preview, write)

    # ------------------------------------------------------------ 查询
    def get_balance(self, account_id: str) -> dict:
        account = self._repo.get_account(account_id)
        if account is None:
            raise AccountNotFound(f"账户 {account_id!r} 不存在")
        log.info("余额查询 account=%s balance=%s", account_id, account.balance)
        return {
            "account_id": account.account_id,
            "balance": money_str(account.balance),
            "balance_minor": int(account.balance),
        }

    # ------------------------------------------------------------ 转账
    def transfer(self, from_id: str, to_id: str, amount_minor: int,
                 idem_key: str) -> dict:
        if not from_id or not to_id:
            raise InvalidAmount("转出/转入账户标识不能为空")
        if from_id == to_id:
            raise InvalidAmount("不能向同一账户转账")
        if amount_minor <= 0:
            raise InvalidAmount("转账金额必须为正数")

        req = {"from": from_id, "to": to_id, "amount_minor": amount_minor}
        preview = {
            "transfer_id": idem_key,
            "from_account_id": from_id,
            "to_account_id": to_id,
            "amount": money_str(amount_minor),
            "amount_minor": amount_minor,
            "status": "completed",
            "replayed": False,
        }

        def write(resp_json: str) -> dict:
            self._repo.transfer(
                idem_key, from_id, to_id, amount_minor, _now_iso(),
                resp_json, canonical_request_hash(req))
            return preview

        return self._run("transfer", idem_key, req, preview, write)

    # ------------------------------------------------------------ 事件
    def list_events(self, account_id: str | None = None) -> list[dict]:
        if account_id is not None and self._repo.get_account(account_id) is None:
            raise AccountNotFound(f"账户 {account_id!r} 不存在")
        return [event_to_dict(e) for e in self._repo.list_events(account_id)]

    # ------------------------------------------------------------ 回放
    def replay(self, account_id: str | None = None) -> dict:
        """只依赖追加事件流重建余额，并与账户表当前余额核对。

        校验三层：
        1. 事件流重算不应出现负余额；
        2. 每条事件记录的 balance_after 必须等于重算值；
        3. 每个账户的重放余额必须等于 accounts 表当前余额。
        """
        if account_id is not None and self._repo.get_account(account_id) is None:
            raise AccountNotFound(f"账户 {account_id!r} 不存在")

        events = self._repo.list_events(account_id)
        rebuilt: dict[str, int] = {}
        for e in events:
            bal = rebuilt.get(e.account_id, 0)
            bal += e.event_type.signed * int(e.amount)
            if bal < 0:
                raise ReplayMismatch(
                    f"事件流推出负余额 account={e.account_id} seq={e.seq}"
                    f" balance={bal}")
            if int(e.balance_after) != bal:
                raise ReplayMismatch(
                    f"事件快照余额不符 account={e.account_id} seq={e.seq}"
                    f" stored={e.balance_after} replayed={bal}")
            rebuilt[e.account_id] = bal

        checked: list[dict] = []
        for acct_id, rebuilt_bal in sorted(rebuilt.items()):
            current = self._repo.get_account(acct_id)
            stored_bal = int(current.balance) if current else None
            if stored_bal != rebuilt_bal:
                raise ReplayMismatch(
                    f"重放余额与账户表不一致 account={acct_id}"
                    f" replayed={rebuilt_bal} stored={stored_bal}")
            checked.append({
                "account_id": acct_id,
                "replayed_balance": money_str(rebuilt_bal),
                "replayed_balance_minor": rebuilt_bal,
                "stored_balance_minor": stored_bal,
                "matches": True,
            })

        total = sum(rebuilt.values())
        log.info("事件回放完成 accounts=%d events=%d total=%s，全部一致",
                 len(checked), len(events), total)
        return {
            "events_replayed": len(events),
            "total_balance": money_str(total),
            "total_balance_minor": total,
            "accounts": checked,
        }

    # -------------------------------------------------------- 幂等内核
    def _run(self, operation: str, idem_key: str | None, request: dict,
             preview: dict, write) -> dict:
        """统一幂等执行流程。

        权威查重发生在仓储的写事务（BEGIN IMMEDIATE）内部，因此对同键的
        并发请求也是原子判定：
        - 无幂等键：直接写入并返回结果；
        - 首次提交：业务变更与幂等记录同一事务落库；
        - 同键同内容（含并发输掉竞争）：仓储抛 IdempotentReplay，
          返回首次落库的响应（标注 replayed），不重复记账；
        - 同键不同内容/跨操作：抛 IdempotencyConflict，绝不覆盖。
        """
        preview_json = json.dumps(preview, ensure_ascii=False)
        if idem_key is None:
            return write(preview_json)

        try:
            write(preview_json)
            log.info("首次提交 key=%s op=%s", idem_key, operation)
            return preview
        except IdempotentReplay as replay:
            # 事务内查重命中：可能是顺序重复，也可能是并发重复
            if replay.operation != operation or \
                    replay.request_hash != canonical_request_hash(request):
                # 理论上仓储已细分，这里做双保险
                raise IdempotencyConflict(
                    f"幂等键 {idem_key!r} 与既有请求不一致，拒绝静默覆盖")
            log.info("幂等命中 key=%s op=%s，返回首次结果，不重复记账",
                     idem_key, operation)
            payload = json.loads(replay.response_json)
            payload["replayed"] = True
            return payload


def event_to_dict(e: LedgerEvent) -> dict:
    return {
        "seq": e.seq,
        "account_id": e.account_id,
        "event_type": e.event_type.value,
        "amount": money_str(e.amount),
        "amount_minor": int(e.amount),
        "balance_after": money_str(e.balance_after),
        "balance_after_minor": int(e.balance_after),
        "transfer_id": e.transfer_id,
        "timestamp": e.timestamp,
    }
