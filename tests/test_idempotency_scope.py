"""业务场景：幂等作用域 —— 开户等写操作也能带提交标识；同一标识用在
不同操作或不同业务对象上各算各的，只有同操作同对象换内容才算冲突。"""

import logging
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from ledger.errors import AccountAlreadyExists, IdempotencyConflict
from ledger.service import LedgerService

logger = logging.getLogger("ledger.tests.idempotency_scope")


def test_create_account_retries_are_idempotent(client: TestClient):
    payload = {"account_id": "a", "initial_balance": "50.00",
               "idempotency_key": "open-1"}
    first = client.post("/accounts", json=payload)
    assert first.status_code == 201

    for _ in range(3):
        again = client.post("/accounts", json=payload)
        assert again.status_code == 201
        assert again.json() == first.json(), "重试开户必须返回与首次一致的结果"
        assert again.headers["x-idempotent-replay"] == "true"

    # 只开了一次户：余额没有重复入金
    assert client.get("/accounts/a").json()["balance"] == "50.00"
    report = client.get("/replay").json()
    assert report["event_count"] == 1  # 仅一条初始入金事件
    assert report["consistent"]
    logger.info("开户重试 3 次，仅首次生效")


def test_create_account_same_key_different_content_conflicts(client: TestClient):
    client.post("/accounts", json={"account_id": "a", "initial_balance": "50.00",
                                   "idempotency_key": "open-2"})
    r = client.post("/accounts", json={"account_id": "a",
                                       "initial_balance": "60.00",
                                       "idempotency_key": "open-2"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    # 冲突请求不产生任何变更
    assert client.get("/accounts/a").json()["balance"] == "50.00"
    logger.info("同键同账户换金额 -> IDEMPOTENCY_CONFLICT")


def test_same_key_on_different_operations_is_independent(client: TestClient):
    """同一个标识分别用于开户、转账、冲正：互不影响、不报冲突。"""
    key = "shared-key"
    assert client.post("/accounts", json={
        "account_id": "a", "initial_balance": "100.00",
        "idempotency_key": key}).status_code == 201
    assert client.post("/accounts", json={
        "account_id": "b", "idempotency_key": key}).status_code == 201

    r = client.post("/transfers", json={
        "from_account": "a", "to_account": "b",
        "amount": "30.00", "idempotency_key": key})
    assert r.status_code == 200
    transfer_id = r.json()["transfer_id"]

    r = client.post(f"/transfers/{transfer_id}/reversal",
                    json={"idempotency_key": key})
    assert r.status_code == 200
    logger.info("同一标识跨开户/转账/冲正复用，均正常执行")

    # 每个操作都真实生效了
    assert client.get("/accounts/a").json()["balance"] == "100.00"
    assert client.get("/accounts/b").json()["balance"] == "0.00"
    assert client.get("/replay").json()["consistent"]


def test_same_key_on_different_accounts_is_independent(client: TestClient):
    """同一标识用于不同账户的开户：各算各的，不互相覆盖。"""
    r1 = client.post("/accounts", json={"account_id": "x",
                                        "initial_balance": "10.00",
                                        "idempotency_key": "open-shared"})
    r2 = client.post("/accounts", json={"account_id": "y",
                                        "initial_balance": "20.00",
                                        "idempotency_key": "open-shared"})
    assert r1.status_code == 201
    assert r2.status_code == 201, "不同业务对象上的同一标识不得误判为冲突"
    assert client.get("/accounts/x").json()["balance"] == "10.00"
    assert client.get("/accounts/y").json()["balance"] == "20.00"
    logger.info("同一标识用于不同账户开户，各自独立生效")


def test_same_key_on_different_transfers_still_conflicts(client: TestClient):
    """转账的业务对象就是提交标识本身：同键换内容仍是真冲突。"""
    client.post("/accounts", json={"account_id": "a", "initial_balance": "100.00"})
    client.post("/accounts", json={"account_id": "b"})
    client.post("/accounts", json={"account_id": "c"})
    base = {"from_account": "a", "to_account": "b",
            "amount": "10.00", "idempotency_key": "pay-scope"}
    assert client.post("/transfers", json=base).status_code == 200
    r = client.post("/transfers", json={**base, "to_account": "c"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_create_account_without_key_keeps_old_behavior(client: TestClient):
    """不带提交标识时，重复开户仍是 ACCOUNT_ALREADY_EXISTS。"""
    client.post("/accounts", json={"account_id": "a"})
    r = client.post("/accounts", json={"account_id": "a"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "ACCOUNT_ALREADY_EXISTS"


def test_concurrent_create_account_with_same_key_creates_once(
    service: LedgerService,
):
    n = 12

    def submit(_: int):
        return service.create_account("a", 5000, "open-concurrent")

    with ThreadPoolExecutor(max_workers=n) as pool:
        results = list(pool.map(submit, range(n)))

    created = [r for r in results if not r[1]]
    replayed = [r for r in results if r[1]]
    assert len(created) == 1, "并发开户只能有一个真正创建"
    assert len(replayed) == n - 1
    assert {r[0].created_at for r in results} == {created[0][0].created_at}
    assert service.get_account("a").balance_cents == 5000
    assert service.replay()["consistent"]
    logger.info("并发 %d 次同键开户，仅 1 次创建，其余幂等重放", n)


def test_concurrent_create_account_same_key_different_content_conflicts(
    service: LedgerService,
):
    """并发同键不同内容：一个成功，其余全部报冲突，不存在第二种结果。"""
    outcomes = []

    def submit(amount: int):
        try:
            _, replayed = service.create_account("a", amount, "open-race")
            return "replayed" if replayed else "created"
        except IdempotencyConflict:
            return "conflict"
        except AccountAlreadyExists:
            return "exists"

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(submit, [1000, 2000, 1000, 2000, 1000, 2000]))

    assert outcomes.count("created") == 1
    # 同内容的并发请求走幂等重放，不同内容的报冲突
    assert set(outcomes) <= {"created", "replayed", "conflict"}
    assert "conflict" in outcomes
    assert service.replay()["consistent"]
    logger.info("并发同键不同内容开户结果: %s", outcomes)
