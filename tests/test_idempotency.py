"""业务场景：幂等提交 —— 重复请求结果一致且不重复记账；
同键换内容必须报可区分的冲突错误。"""

import logging
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from ledger.errors import IdempotencyConflict
from ledger.service import LedgerService

logger = logging.getLogger("ledger.tests.idempotency")


def test_duplicate_submission_returns_same_result_without_double_booking(
    client: TestClient,
):
    client.post("/accounts", json={"account_id": "a", "initial_balance": "100.00"})
    client.post("/accounts", json={"account_id": "b"})
    payload = {
        "from_account": "a", "to_account": "b",
        "amount": "25.00", "idempotency_key": "pay-001",
    }

    first = client.post("/transfers", json=payload)
    assert first.status_code == 200
    for i in range(3):
        again = client.post("/transfers", json=payload)
        assert again.status_code == 200
        assert again.json() == first.json(), "重复提交必须返回与首次一致的结果"
        assert again.headers["x-idempotent-replay"] == "true"

    logger.info("重复提交 3 次，响应体与首次完全一致")
    # 不重复记账：余额只变动一次
    assert client.get("/accounts/a").json()["balance"] == "75.00"
    assert client.get("/accounts/b").json()["balance"] == "25.00"
    # 事件只有一借一贷两条（a 的初始入金另算 1 条）
    report = client.get("/replay").json()
    assert report["event_count"] == 3
    assert report["consistent"]


def test_same_key_different_content_conflicts(client: TestClient):
    client.post("/accounts", json={"account_id": "a", "initial_balance": "100.00"})
    client.post("/accounts", json={"account_id": "b"})
    client.post("/accounts", json={"account_id": "c"})
    base = {"from_account": "a", "to_account": "b",
            "amount": "10.00", "idempotency_key": "pay-002"}
    assert client.post("/transfers", json=base).status_code == 200

    variants = [
        {**base, "amount": "20.00"},          # 金额不同
        {**base, "to_account": "c"},          # 收款方不同
        {**base, "from_account": "b", "to_account": "a"},  # 方向不同
    ]
    for v in variants:
        r = client.post("/transfers", json=v)
        assert r.status_code == 409
        body = r.json()
        assert body["error"]["code"] == "IDEMPOTENCY_CONFLICT"
        assert "different request" in body["error"]["message"]
        logger.info("同键换内容被拒绝: %s", body["error"]["message"])

    # 冲突请求不得产生任何记账
    assert client.get("/accounts/b").json()["balance"] == "10.00"
    assert client.get("/replay").json()["consistent"]


def test_concurrent_duplicate_submissions_record_once(service: LedgerService):
    """同一请求并发提交 N 次：只记账一次，其余返回首次结果。"""
    service.create_account("a", 10000)
    service.create_account("b", 0)
    n = 12

    def submit(_: int):
        return service.transfer("a", "b", 500, "pay-concurrent")

    with ThreadPoolExecutor(max_workers=n) as pool:
        results = list(pool.map(submit, range(n)))

    bodies = {r[1]["transfer_id"] for r in results}
    assert len(bodies) == 1, "所有并发重复提交必须得到同一个 transfer_id"
    assert sum(1 for r in results if not r[2]) == 1, "只能有一个请求真正记账"
    logger.info("并发 %d 次重复提交，仅 1 次记账", n)
    assert service.get_account("a").balance_cents == 9500
    assert service.get_account("b").balance_cents == 500
    assert service.replay()["consistent"]


def test_idempotency_keys_are_independent(service: LedgerService):
    service.create_account("a", 10000)
    service.create_account("b", 0)
    service.transfer("a", "b", 100, "key-1")
    service.transfer("a", "b", 100, "key-2")  # 不同键 = 不同业务请求
    assert service.get_account("b").balance_cents == 200
    with pytest.raises(IdempotencyConflict):
        service.transfer("a", "b", 200, "key-1")
