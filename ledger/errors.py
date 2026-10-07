"""归一化的业务错误类型。

所有底层异常（sqlite3 驱动错误、框架错误等）在 service 层被捕获并
转换为这里的 LedgerError 子类，api 层再统一映射为 HTTP 响应，
保证调用方永远不会看到驱动或框架的原始报错。
"""

from __future__ import annotations


class LedgerError(Exception):
    """所有账本业务错误的基类。"""

    code: str = "LEDGER_ERROR"
    http_status: int = 500

    def __init__(self, message: str = "") -> None:
        super().__init__(message)
        self.message = message or self.code

    def to_dict(self) -> dict:
        return {"error": {"code": self.code, "message": self.message}}


class AccountNotFound(LedgerError):
    code = "ACCOUNT_NOT_FOUND"
    http_status = 404


class AccountAlreadyExists(LedgerError):
    code = "ACCOUNT_ALREADY_EXISTS"
    http_status = 409


class InsufficientFunds(LedgerError):
    code = "INSUFFICIENT_FUNDS"
    http_status = 422


class InvalidAmount(LedgerError):
    code = "INVALID_AMOUNT"
    http_status = 422


class SameAccountTransfer(LedgerError):
    code = "SAME_ACCOUNT_TRANSFER"
    http_status = 422


class InvalidRequest(LedgerError):
    """请求体本身不合法（字段缺失、类型不对等）。"""

    code = "INVALID_REQUEST"
    http_status = 422


class TransferNotFound(LedgerError):
    """冲正目标转账不存在。"""

    code = "TRANSFER_NOT_FOUND"
    http_status = 404


class TransferAlreadyReversed(LedgerError):
    """目标转账已经被冲正过（一笔转账最多冲正一次）。"""

    code = "TRANSFER_ALREADY_REVERSED"
    http_status = 409


class IdempotencyConflict(LedgerError):
    """同一幂等键被用于不同的请求内容。"""

    code = "IDEMPOTENCY_CONFLICT"
    http_status = 409


class StorageError(LedgerError):
    """底层存储异常归一化后的类型（不暴露驱动细节）。"""

    code = "STORAGE_ERROR"
    http_status = 500
