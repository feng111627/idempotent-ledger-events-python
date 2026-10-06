"""领域模型：金额、账户、资金事件。

金额一律以整数「最小货币单位」（如分）存储与运算，
全程不使用 float，避免浮点误差导致对账不平。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import NewType

# 金额以最小货币单位的非负整数表示
Amount = NewType("Amount", int)


class EventType(str, Enum):
    """资金事件类型。"""

    OPEN = "open"        # 开户入账
    TRANSFER_OUT = "transfer_out"
    TRANSFER_IN = "transfer_in"


@dataclass(frozen=True, slots=True)
class Account:
    account_id: str
    balance: Amount  # 最小货币单位


@dataclass(frozen=True, slots=True)
class LedgerEvent:
    """追加式资金事件，事件流回放即可重建余额。"""

    seq: int                 # 全局单调递增序号
    account_id: str
    event_type: EventType
    amount: Amount           # 恒为非负；方向由 event_type 决定
    balance_after: Amount    # 该事件应用后的账户余额
    transfer_id: str | None  # 关联的转账幂等标识
    timestamp: str


class Money:
    """金额值对象：字符串/整数 -> 最小货币单位整数。"""

    __slots__ = ("_minor",)

    def __init__(self, minor: int) -> None:
        self._minor = minor

    @classmethod
    def from_str(cls, text: str) -> "Money":
        raise NotImplementedError

    @classmethod
    def from_minor(cls, minor: int) -> "Money":
        raise NotImplementedError

    @property
    def minor(self) -> int:
        return self._minor

    def to_decimal_str(self) -> str:
        raise NotImplementedError
