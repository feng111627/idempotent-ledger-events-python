"""HTTP 请求/响应模型。金额以十进制字符串收发，服务端转整数。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class CreateAccountRequest(BaseModel):
    account_id: str = Field(min_length=1, max_length=128)
    opening_balance: str = Field(default="0", description="十进制金额字符串，如 100.00")
    idempotency_key: str | None = Field(default=None, max_length=128)


class TransferRequest(BaseModel):
    from_account_id: str = Field(min_length=1, max_length=128)
    to_account_id: str = Field(min_length=1, max_length=128)
    amount: str = Field(description="正数十进制金额字符串，如 12.30")
    idempotency_key: str = Field(min_length=1, max_length=128)


class BalanceResponse(BaseModel):
    account_id: str
    balance: str
    balance_minor: int
