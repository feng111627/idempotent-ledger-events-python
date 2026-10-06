"""业务：开户、余额查询、金额精度与非法输入分类。"""
from __future__ import annotations

import logging

import pytest

from app.domain import Money
from app.errors import InvalidAmount

log = logging.getLogger("ledger.test.accounts")


# ------------------------------------------------------------------ 金额
class TestMoneyPrecision:
    """金额必须走十进制字符串 + 整数最小单位，杜绝浮点误差。"""

    @pytest.mark.parametrize("text,minor", [
        ("0", 0), ("0.00", 0), ("0.01", 1), ("100", 10000),
        ("12.30", 1230), ("999999.99", 99999999),
    ])
    def test_parse_decimal_string_to_minor(self, text, minor):
        got = Money.from_str(text).minor
        log.info("金额解析 %s -> %d 最小单位（期望 %d）", text, got, minor)
        assert got == minor

    @pytest.mark.parametrize("minor,text", [
        (0, "0.00"), (1, "0.01"), (1230, "12.30"), (10000, "100.00"),
    ])
    def test_render_minor_to_decimal(self, minor, text):
        assert Money.from_minor(minor).to_decimal_str() == text

    @pytest.mark.parametrize("bad", ["", "abc", "1.2.3", "1.234",
                                     "NaN", "Infinity", "-Infinity"])
    def test_reject_illegal_amount(self, bad):
        log.info("非法金额 %r 应归一化为 invalid_amount", bad)
        with pytest.raises(InvalidAmount):
            Money.from_str(bad)

    def test_no_float_rounding_error(self):
        """经典的 0.1+0.2 浮点问题在整数运算下不存在。"""
        total = Money.from_str("0.10").minor + Money.from_str("0.20").minor
        assert total == 30
        log.info("0.10 + 0.20 = %d 最小单位（精确，无 0.30000000000000004）", total)


# ------------------------------------------------------------------ 账户
class TestAccountLifecycle:
    def test_open_account_and_query(self, client):
        log.info("[开户] A 初始 100.00")
        r = client.post("/accounts", json={
            "account_id": "A", "opening_balance": "100.00",
            "idempotency_key": "open-A"})
        assert r.status_code == 201
        body = r.json()
        assert body["balance"] == "100.00" and body["replayed"] is False

        g = client.get("/accounts/A")
        assert g.status_code == 200
        assert g.json() == {"account_id": "A",
                            "balance": "100.00", "balance_minor": 10000}
        log.info("[查询] A 余额 %s", g.json()["balance"])

    def test_duplicate_account_id_conflicts(self, client):
        client.post("/accounts", json={"account_id": "A",
                    "opening_balance": "10", "idempotency_key": "k1"})
        # 换一个幂等键再开同名账户 -> 业务冲突，而不是静默覆盖
        r = client.post("/accounts", json={"account_id": "A",
                        "opening_balance": "10", "idempotency_key": "k2"})
        log.info("[重复开户] 状态码=%s 错误码=%s",
                 r.status_code, r.json()["error"]["code"])
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "account_already_exists"

    def test_query_unknown_account_is_404(self, client):
        r = client.get("/accounts/ghost")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "account_not_found"

    def test_negative_opening_balance_rejected(self, client):
        r = client.post("/accounts", json={"account_id": "X",
                        "opening_balance": "-1.00", "idempotency_key": "k"})
        log.info("[负金额开户] 状态码=%s 错误码=%s",
                 r.status_code, r.json()["error"]["code"])
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "invalid_amount"

    def test_amount_precision_rejected_at_boundary(self, client):
        r = client.post("/accounts", json={"account_id": "X",
                        "opening_balance": "1.234", "idempotency_key": "k"})
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "invalid_amount"
