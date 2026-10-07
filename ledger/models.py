"""数据模型与金额类型定义。

金额规则：
- 内部一律使用整数“分”（cents）存储与计算，杜绝浮点误差。
- API 边界接受字符串形式的十进制金额（如 "12.34"），
  通过 Decimal 精确解析为分，超过两位小数直接拒绝。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .errors import InvalidAmount

CENT = Decimal("0.01")


def parse_amount_to_cents(raw: str | int | float | Decimal, *, allow_zero: bool = False) -> int:
    """把外部输入的金额解析为整数分；非法输入抛 InvalidAmount。

    默认只接受正数（转账金额）；``allow_zero=True`` 时 0、0.0、0.00
    等常见零金额写法都会被归一化为 0 放行（开户初始金额）。
    """
    if isinstance(raw, bool):
        raise InvalidAmount("amount must be a decimal number")
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        raise InvalidAmount(f"amount is not a valid decimal: {raw!r}") from None
    if value.is_nan() or value.is_infinite():
        raise InvalidAmount("amount must be finite")
    try:
        quantized = value.quantize(CENT)
    except InvalidOperation:
        # 超出 Decimal 上下文精度（如 1e999）
        raise InvalidAmount("amount is out of representable range") from None
    if value != quantized:
        raise InvalidAmount("amount must have at most 2 decimal places")
    if value < 0:
        raise InvalidAmount("amount must not be negative")
    if not allow_zero and value == 0:
        raise InvalidAmount("amount must be positive")
    return int(value * 100)


def parse_initial_balance_to_cents(raw: str | int | float | Decimal) -> int:
    """开户初始金额：允许 0、0.0、0.00 等零金额写法，统一归一化为 0。"""
    return parse_amount_to_cents(raw, allow_zero=True)


def format_cents(cents: int) -> str:
    """把整数分格式化为字符串金额，如 1234 -> "12.34"。"""
    return str((Decimal(cents) / 100).quantize(CENT))


@dataclass(frozen=True)
class Account:
    account_id: str
    balance_cents: int
    created_at: str


@dataclass(frozen=True)
class LedgerEvent:
    event_id: int
    transfer_id: str
    account_id: str
    kind: str  # "debit" | "credit" | "reversal_debit" | "reversal_credit"
    delta_cents: int  # 带符号：debit 为负，credit 为正；冲正方向相反
    balance_after_cents: int
    idempotency_key: str
    created_at: str


@dataclass(frozen=True)
class TransferRecord:
    """一次成功转账的汇总信息（从事件流聚合得到）。"""

    transfer_id: str
    from_account: str
    to_account: str
    amount_cents: int


@dataclass(frozen=True)
class Reversal:
    """一次成功冲正的记录。"""

    reversal_id: str
    transfer_id: str
    account_id: str  # 资金回退到的付款方
    amount_cents: int
    idempotency_key: str
    created_at: str
