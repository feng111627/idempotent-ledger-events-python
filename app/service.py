"""业务服务层：账户、转账、幂等与事件回放。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict

from app.domain import Account, Amount, EventType, LedgerEvent, Money
from app.errors import AccountNotFound, InvalidAmount
from app.repository import LedgerRepository


def canonical_request_hash(payload: dict) -> str:
    """对请求内容做规范化哈希，用于识别「同 key 换内容」。"""
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class LedgerService:
    def __init__(self, repo: LedgerRepository) -> None:
        self._repo = repo

    def create_account(self, account_id: str, opening_minor: int,
                       idem_key: str | None = None) -> dict:
        raise NotImplementedError

    def get_balance(self, account_id: str) -> dict:
        raise NotImplementedError

    def transfer(self, from_id: str, to_id: str, amount_minor: int,
                 idem_key: str) -> dict:
        raise NotImplementedError

    def replay(self, account_id: str | None = None) -> dict:
        """只依赖追加事件流重建余额，并与账户表当前余额核对。"""
        raise NotImplementedError


def event_to_dict(e: LedgerEvent) -> dict:
    raise NotImplementedError
