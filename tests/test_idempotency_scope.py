"""业务场景：提交标识作用于所有写操作（含开户）。

同一标识在不同操作 / 不同业务对象上各算各的；只有同一操作、同一对象
但请求内容变了才算真冲突。
"""

import logging
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from ledger.errors import IdempotencyConflict
from ledger.service import LedgerService

logger = logging.getLogger("ledger.tests.idempotency_scope")


def test_create_account_idempotent_retry_opens_once(client: TestClient):
    payload = {"account_id": "a", "initial_balance": "50.00",
               "idempotency_key": "open-1"}
    first = client.post("/accounts", json=payload)
    assert first.status_code == 201
    for _ in range(3):
        again = client.post("/accounts", json=payload)
        assert again.status_code == 201
        assert again.json() == first.json(), "重试开户必须返回首次结果"
        assert again.headers["x-idempotent-replay"] == "true"

    assert client.get("/accounts/a").json()["balance"] == "50.00"
    # 初始入金事件只可能有一条
    assert client.get("/replay", params={"account_id": "a"}).json()["event_count"] == 1
    logger.info("开户重试 3 次：只开一次，响应一致")


def test_concurrent_create_account_with_same_key_opens_once(
    service: LedgerService,
):
    n = 12

    def submit(_: int):
        return service.create_account("a", 1000, "open-concurrent")

    with ThreadPoolExecutor(max_workers=n) as pool:
        results = list(pool.map(submit, range(n)))

    accounts = {(a.account_id, a.created_at) for a, _ in results}
    assert len(accounts) == 1
    assert sum(1 for _, replayed in results if not replayed) == 1
    logger.info("并发 %d 次同键开户：仅 1 次真正创建", n)
    assert service.get_account("a").balance_cents == 1000
    assert service.replay()["consistent"]


def test_same_key_different_accounts_are_independent(client: TestClient):
    r1 = client.post("/accounts", json={
        "account_id": "a", "initial_balance": "10.00", "idempotency_key": "k",
    })
    r2 = client.post("/accounts", json={
        "account_id": "b", "initial_balance": "20.00", "idempotency_key": "k",
    })
    assert r1.status_code == 201 and r2.status_code == 201
    # 各自重放返回各自的首次结果
    r1_again = client.post("/accounts", json={
        "account_id": "a", "initial_balance": "10.00", "idempotency_key": "k",
    })
    r2_again = client.post("/accounts", json={
        "account_id": "b", "initial_balance": "20.00", "idempotency_key": "k",
    })
    assert r1_again.json() == r1.json()
    assert r2_again.json() == r2.json()
    assert client.get("/accounts/a").json()["balance"] == "10.00"
    assert client.get("/accounts/b").json()["balance"] == "20.00"
    logger.info("同键开不同账户：互不覆盖、各自幂等")


def test_same_key_same_account_different_initial_balance_conflicts(
    client: TestClient,
):
    client.post("/accounts", json={
        "account_id": "a", "initial_balance": "10.00", "idempotency_key": "k",
    })
    r = client.post("/accounts", json={
        "account_id": "a", "initial_balance": "99.00", "idempotency_key": "k",
    })
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    # 冲突不改写已有结果
    assert client.get("/accounts/a").json()["balance"] == "10.00"
    logger.info("同键同账户换初始金额 -> IDEMPOTENCY_CONFLICT")


def test_same_key_across_different_operations_are_independent(
    service: LedgerService,
):
    """同一个 key 同时用于开户和转账：操作不同，各算各的。"""
    service.create_account("a", 10000, "shared")
    service.create_account("b", 0)
    status, body, replayed = service.transfer("a", "b", 500, "shared")
    assert status == 200 and replayed is False
    # 再用同一个 key 重放开户与转账，各自拿到自己的首次结果
    _, account_replay = service.create_account("a", 10000, "shared")
    _, _, transfer_replay = service.transfer("a", "b", 500, "shared")
    assert account_replay is True
    assert transfer_replay is True

    # 但同 key 同操作对象换内容仍然是真冲突
    import pytest
    with pytest.raises(IdempotencyConflict):
        service.transfer("a", "b", 600, "shared")
    logger.info("同键跨操作互不干扰，同操作同对象换内容仍冲突")
