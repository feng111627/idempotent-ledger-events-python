"""业务场景：失败原因分类 —— 每类错误有可区分的 code 与 HTTP 状态。"""

import logging

import pytest
from fastapi.testclient import TestClient

logger = logging.getLogger("ledger.tests.errors")


@pytest.mark.parametrize("amount", ["abc", "1.001", "-5", "0", "1e999"])
def test_invalid_amount_rejected(client: TestClient, amount: str):
    client.post("/accounts", json={"account_id": "a", "initial_balance": "10.00"})
    client.post("/accounts", json={"account_id": "b"})
    r = client.post("/transfers", json={
        "from_account": "a", "to_account": "b",
        "amount": amount, "idempotency_key": f"bad-{amount}",
    })
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_AMOUNT"
    logger.info("非法金额 %r -> INVALID_AMOUNT", amount)


def test_account_not_found(client: TestClient):
    r = client.get("/accounts/ghost")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "ACCOUNT_NOT_FOUND"

    client.post("/accounts", json={"account_id": "a", "initial_balance": "1.00"})
    r = client.post("/transfers", json={
        "from_account": "a", "to_account": "ghost",
        "amount": "0.50", "idempotency_key": "nf-1",
    })
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "ACCOUNT_NOT_FOUND"


def test_insufficient_funds(client: TestClient):
    client.post("/accounts", json={"account_id": "a", "initial_balance": "1.00"})
    client.post("/accounts", json={"account_id": "b"})
    r = client.post("/transfers", json={
        "from_account": "a", "to_account": "b",
        "amount": "2.00", "idempotency_key": "is-1",
    })
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "INSUFFICIENT_FUNDS"
    assert "1.00" in body["error"]["message"]
    logger.info("透支被拒绝: %s", body["error"]["message"])


def test_same_account_transfer_rejected(client: TestClient):
    client.post("/accounts", json={"account_id": "a", "initial_balance": "1.00"})
    r = client.post("/transfers", json={
        "from_account": "a", "to_account": "a",
        "amount": "0.50", "idempotency_key": "sa-1",
    })
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "SAME_ACCOUNT_TRANSFER"


def test_duplicate_account_rejected(client: TestClient):
    client.post("/accounts", json={"account_id": "a"})
    r = client.post("/accounts", json={"account_id": "a"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "ACCOUNT_ALREADY_EXISTS"


def test_unknown_error_is_normalized(client: TestClient):
    """未预期异常不得把框架/驱动原始报错暴露给调用方。"""
    from ledger.api import create_app

    # ServerErrorMiddleware 处理后会重抛异常，测试客户端需关闭重抛
    safe_client = TestClient(create_app(":memory:"), raise_server_exceptions=False)
    service = safe_client.app.state.service

    def boom(*args, **kwargs):
        raise RuntimeError("sqlite3.OperationalError: secret internal detail")

    original = service.get_account
    service.get_account = boom
    try:
        r = safe_client.get("/accounts/a")
    finally:
        service.get_account = original
    assert r.status_code == 500
    body = r.json()
    assert body["error"]["code"] == "STORAGE_ERROR"
    assert "secret internal detail" not in r.text
    assert "RuntimeError" not in r.text
    logger.info("未知异常已归一化: %s", body)
