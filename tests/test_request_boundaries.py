"""业务场景：边界与一致性细节 —— 零金额的常见写法一视同仁；
非法请求体的错误结构与业务错误完全一致。"""

import logging

import pytest
from fastapi.testclient import TestClient

logger = logging.getLogger("ledger.tests.boundaries")


@pytest.mark.parametrize("zero", ["0", "0.0", "0.00", "0.000"])
def test_zero_initial_balance_spellings_all_accepted(client: TestClient, zero: str):
    r = client.post("/accounts", json={"account_id": f"z{len(zero)}-{zero.count('0')}",
                                       "initial_balance": zero})
    assert r.status_code == 201, f"零金额写法 {zero!r} 必须被放行"
    assert r.json()["balance"] == "0.00"
    logger.info("零金额写法 %r -> 开户成功, 余额 0.00", zero)


def test_zero_opening_balance_records_no_event(client: TestClient):
    client.post("/accounts", json={"account_id": "z", "initial_balance": "0"})
    report = client.get("/replay").json()
    assert report["event_count"] == 0, "零开户不应产生入金事件"
    assert report["consistent"]
    assert report["accounts"][0]["stored_balance"] == "0.00"
    assert report["accounts"][0]["rebuilt_balance"] == "0.00"


def test_transfer_zero_amount_still_rejected(client: TestClient):
    """零金额放行只针对开户；转账金额仍必须为正。"""
    client.post("/accounts", json={"account_id": "a", "initial_balance": "10.00"})
    client.post("/accounts", json={"account_id": "b"})
    for zero in ["0", "0.00"]:
        r = client.post("/transfers", json={
            "from_account": "a", "to_account": "b",
            "amount": zero, "idempotency_key": f"zero-{zero}"})
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "INVALID_AMOUNT"
    logger.info("转账零金额仍被拒绝")


def test_negative_initial_balance_rejected(client: TestClient):
    r = client.post("/accounts", json={"account_id": "neg",
                                       "initial_balance": "-0.01"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_AMOUNT"


# ---- 非法请求体：错误结构必须与业务错误完全一致 ----

BUSINESS_ERROR_SHAPE = {"error": {"code", "message"}}


def _assert_error_shape(resp, expected_code: str):
    assert resp.status_code == 422
    body = resp.json()
    assert set(body.keys()) == {"error"}, f"错误结构异常: {body}"
    assert set(body["error"].keys()) == {"code", "message"}
    assert body["error"]["code"] == expected_code
    assert isinstance(body["error"]["message"], str)


def test_missing_field_uses_unified_error_shape(client: TestClient):
    r = client.post("/accounts", json={"initial_balance": "1.00"})  # 缺 account_id
    _assert_error_shape(r, "INVALID_REQUEST")
    logger.info("缺字段 -> %s", r.json())

    r = client.post("/transfers", json={"from_account": "a", "to_account": "b"})
    _assert_error_shape(r, "INVALID_REQUEST")  # 缺 amount / idempotency_key


def test_wrong_type_uses_unified_error_shape(client: TestClient):
    r = client.post("/accounts", json={"account_id": 123})  # 类型不对
    _assert_error_shape(r, "INVALID_REQUEST")

    r = client.post("/transfers", json={
        "from_account": "a", "to_account": "b",
        "amount": {"value": 1}, "idempotency_key": "t"})  # amount 应为字符串
    _assert_error_shape(r, "INVALID_REQUEST")
    logger.info("类型错误 -> %s", r.json())


def test_malformed_json_uses_unified_error_shape(client: TestClient):
    r = client.post("/accounts", content=b"{not-json",
                    headers={"content-type": "application/json"})
    _assert_error_shape(r, "INVALID_REQUEST")


def test_validation_error_shape_matches_business_error_shape(client: TestClient):
    """非法请求体与业务错误的 JSON 结构键完全一致。"""
    validation = client.post("/accounts", json={}).json()
    business = client.post("/accounts", json={"account_id": "a",
                                              "initial_balance": "abc"}).json()
    assert set(validation.keys()) == set(business.keys()) == {"error"}
    assert set(validation["error"].keys()) == set(business["error"].keys())
    assert validation["error"]["code"] == "INVALID_REQUEST"
    assert business["error"]["code"] == "INVALID_AMOUNT"
    logger.info("校验错误与业务错误结构一致: %s", validation)
