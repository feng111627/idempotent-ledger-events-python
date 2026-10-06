"""领域模型：金额、账户、资金事件。

金额一律以整数「最小货币单位」（如分）存储与运算，
全程不使用 float，避免浮点误差导致对账不平。
HTTP 边界收发十进制字符串，进入领域层立即转为整数。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import NewType

from app.errors import InvalidAmount

# 金额以最小货币单位的整数表示（1 主币单位 = 100 最小单位）
Amount = NewType("Amount", int)

CENT_SCALE = 100
DECIMAL_PLACES = 2


class EventType(str, Enum):
    """资金事件类型。"""

    OPEN = "open"          # 开户入账
    TRANSFER_OUT = "transfer_out"
    TRANSFER_IN = "transfer_in"

    @property
    def signed(self) -> int:
        """对账户余额的作用方向：+1 入账，-1 出账。"""
        return 1 if self in (EventType.OPEN, EventType.TRANSFER_IN) else -1


@dataclass(frozen=True, slots=True)
class Account:
    account_id: str
    balance: Amount  # 最小货币单位，恒非负


@dataclass(frozen=True, slots=True)
class LedgerEvent:
    """追加式资金事件，事件流回放即可重建余额。"""

    seq: int                 # 全局单调递增序号
    account_id: str
    event_type: EventType
    amount: Amount           # 恒为非负；方向由 event_type 决定
    balance_after: Amount    # 该事件应用后的账户余额
    transfer_id: str | None  # 关联的转账幂等标识（开户事件为 None）
    timestamp: str


class Money:
    """金额值对象：十进制字符串 <-> 最小货币单位整数。

    转换基于 decimal.Decimal，绝不经过 float；
    超过 DECIMAL_PLACES 位小数、NaN/Infinity 等非法输入直接拒绝。
    """

    __slots__ = ("_minor",)

    def __init__(self, minor: int) -> None:
        self._minor = int(minor)

    @classmethod
    def from_str(cls, text: str) -> "Money":
        if not isinstance(text, str) or not text.strip():
            raise InvalidAmount("金额必须是非空十进制字符串，例如 '12.30'")
        try:
            d = Decimal(text.strip())
        except InvalidOperation:
            raise InvalidAmount(f"无法解析的金额: {text!r}") from None
        if not d.is_finite():
            raise InvalidAmount(f"金额必须是有限数: {text!r}")
        if d.as_tuple().exponent < -DECIMAL_PLACES:
            raise InvalidAmount(
                f"金额最多支持 {DECIMAL_PLACES} 位小数: {text!r}")
        return cls(int(d * CENT_SCALE))  # Decimal 精确乘法后取整

    @classmethod
    def from_minor(cls, minor: int) -> "Money":
        return cls(minor)

    @property
    def minor(self) -> int:
        return self._minor

    def to_decimal_str(self) -> str:
        """还原为固定两位小数字符串，如 1230 -> '12.30'。"""
        sign = "-" if self._minor < 0 else ""
        v = abs(self._minor)
        return f"{sign}{v // CENT_SCALE}.{v % CENT_SCALE:02d}"
