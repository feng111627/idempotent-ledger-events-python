"""业务边界：开户零金额的各种写法 + 非法请求体的错误结构归一化。"""

import logging

import pytest
from fastapi.testclient import TestClient

logger = logging.getLogger("ledger.tests.boundaries")


@pytest.mark.parametrize("zero", ["0", "0.0", "0.00", "000", 0, 0.0])
def test_zero_initial_balance_variants_accepted(client: TestClient, zero):
    r = client.post("/accounts", json={"account_id": "x", "initial_balance": zero})
    assert r.status_code == 201, r.text
    assert r.json()["balance"] == "0.00"
    report = client.get("/replay", params={"account_id": "x"}).json()
    assert report["consistent"]
    # 零初始金额不产生资金事件
    assert report["event_count"] == 0
    logger.info("零金额写法 %r 统一放行，归一化为 0.00", zero)


def test_zero_transfer_amount_still_rejected(client: TestClient):
    client.post("/accounts", json={"account_id": "a", "initial_balance": "10.00"})
    client.post("/accounts", json={"account_id": "b"})
    for zero in ["0", "0.0", "0.00"]:
        r = client.post("/transfers", json={
            "from_account": "a", "to_account": "b",
            "amount": zero, "idempotency_key": f"zero-{zero}",
        })
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "INVALID_AMOUNT"
    logger.info("转账金额为 0 仍然拒绝（只是开户初始金额允许 0）")


@pytest.mark.parametrize("payload", [
    {},  # 缺 account_id
    {"initial_balance": "1.00"},  # 缺 account_id
    {"account_id": 123},  # 类型不对
    {"account_id": "a", "initial_balance": [1]},  # 类型不对
])
def test_invalid_create_account_body_uses_business_error_shape(
    client: TestClient, payload
):
    r = client.post("/accounts", json=payload)
    assert r.status_code == 422
    body = r.json()
    assert set(body.keys()) == {"error"}
    assert set(body["error"].keys()) == {"code", "message"}
    assert body["error"]["code"] == "INVALID_REQUEST"
    logger.info("非法开户请求 -> 统一错误结构 INVALID_REQUEST: %s",
                body["error"]["message"])


@pytest.mark.parametrize("payload", [
    {},  # 全缺
    {"from_account": "a", "to_account": "b", "amount": "1.00"},  # 缺 key
    {"from_account": "a", "to_account": "b",
     "amount": 1, "idempotency_key": 5},  # key 类型不对
    {"from_account": ["a"], "to_account": "b",
     "amount": "1.00", "idempotency_key": "k"},  # account 类型不对
    {"from_account": "", "to_account": "b",
     "amount": "1.00", "idempotency_key": "k"},  # 空字符串
])
def test_invalid_transfer_body_uses_business_error_shape(
    client: TestClient, payload
):
    r = client.post("/transfers", json=payload)
    assert r.status_code == 422
    body = r.json()
    assert set(body.keys()) == {"error"}
    assert body["error"]["code"] == "INVALID_REQUEST"
    logger.info("非法转账请求 -> INVALID_REQUEST")


def test_invalid_reversal_body_uses_business_error_shape(client: TestClient):
    r = client.post("/transfers/t1/reversal", json={"idempotency_key": 7})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_REQUEST"

    r2 = client.post("/transfers/t1/reversal", json={})
    assert r2.status_code == 422
    assert r2.json()["error"]["code"] == "INVALID_REQUEST"


def test_invalid_json_body_uses_business_error_shape(client: TestClient):
    """请求体不是合法 JSON 时，结构也必须与业务错误一致。"""
    r = client.post(
        "/accounts",
        content=b"{not json",
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 422
    body = r.json()
    assert set(body.keys()) == {"error"}
    assert body["error"]["code"] == "INVALID_REQUEST"
    logger.info("非法 JSON -> INVALID_REQUEST 统一结构")
