"""归一化错误分类。

所有仓储/数据库驱动抛出的底层异常，都会被翻译成这里定义的
LedgerError 子类，绝不把 sqlite3 等驱动的原始报错透出给调用方。
"""
from __future__ import annotations


class LedgerError(Exception):
    """所有账本错误的基类。"""

    code: str = "ledger_error"
    http_status: int = 400


class AccountNotFound(LedgerError):
    code = "account_not_found"
    http_status = 404


class AccountAlreadyExists(LedgerError):
    code = "account_already_exists"
    http_status = 409


class InsufficientFunds(LedgerError):
    code = "insufficient_funds"
    http_status = 422


class InvalidAmount(LedgerError):
    code = "invalid_amount"
    http_status = 422


class IdempotencyConflict(LedgerError):
    """同一幂等标识被用于不同的请求内容。"""

    code = "idempotency_conflict"
    http_status = 409


class IdempotentReplay(LedgerError):
    """非错误的控制信号：事务内权威查重发现该键已被（并发地）提交。

    携带首次落库的 operation/request_hash/response_json，供服务层
    返回与首次一致的结果，而不是重复记账。
    """

    code = "idempotent_replay"
    http_status = 200

    def __init__(self, operation: str, request_hash: str,
                 response_json: str) -> None:
        super().__init__("该幂等键已提交")
        self.operation = operation
        self.request_hash = request_hash
        self.response_json = response_json


class StorageUnavailable(LedgerError):
    """底层存储异常的归一化包装（死锁/IO 等）。"""

    code = "storage_unavailable"
    http_status = 503


class ReplayMismatch(LedgerError):
    """事件流重放余额与账户表不一致（账本已被破坏）。"""

    code = "replay_mismatch"
    http_status = 500
