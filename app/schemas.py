"""HTTP 请求/响应模型。金额以十进制字符串收发，进入服务前转整数。"""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from app.domain import Money


class CreateAccountRequest(BaseModel):
    account_id: str = Field(min_length=1, max_length=128)
    opening_balance: str = Field(default="0",
                                 description="十进制金额字符串，如 100.00")
    idempotency_key: str | None = Field(default=None, max_length=128)

    @field_validator("opening_balance")
    @classmethod
    def _check_amount(cls, v: str) -> str:
        Money.from_str(v)  # 解析校验（拒绝 float 风格之外的非法输入）
        return v


class TransferRequest(BaseModel):
    from_account_id: str = Field(min_length=1, max_length=128)
    to_account_id: str = Field(min_length=1, max_length=128)
    amount: str = Field(description="正数十进制金额字符串，如 12.30")
    idempotency_key: str = Field(min_length=1, max_length=128)

    @field_validator("amount")
    @classmethod
    def _check_amount(cls, v: str) -> str:
        Money.from_str(v)
        return v


class BalanceResponse(BaseModel):
    account_id: str
    balance: str
    balance_minor: int
