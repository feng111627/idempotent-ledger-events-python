"""业务场景：冲正/退款 —— 已成功的转账可全额冲正一次，钱退回原付款方；
只追加事件不改写历史；重复提交与并发冲正都不会多退或冲成负数。"""

import logging
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from ledger.errors import (
    InsufficientFunds,
    TransferAlreadyReversed,
    TransferNotFound,
)
from ledger.service import LedgerService
from ledger.store import Store

logger = logging.getLogger("ledger.tests.reversal")


def _open_and_transfer(client: TestClient, amount: str = "40.00") -> str:
    client.post("/accounts", json={"account_id": "payer", "initial_balance": "100.00"})
    client.post("/accounts", json={"account_id": "payee"})
    r = client.post("/transfers", json={
        "from_account": "payer", "to_account": "payee",
        "amount": amount, "idempotency_key": "pay-to-reverse",
    })
    assert r.status_code == 200
    return r.json()["transfer_id"]


def test_reversal_refunds_payer_and_history_is_append_only(client: TestClient):
    transfer_id = _open_and_transfer(client)
    assert client.get("/accounts/payer").json()["balance"] == "60.00"
    assert client.get("/accounts/payee").json()["balance"] == "40.00"

    r = client.post(f"/transfers/{transfer_id}/reversal",
                    json={"idempotency_key": "rev-1"})
    assert r.status_code == 200
    body = r.json()
    assert body["transfer_id"] == transfer_id
    assert body["amount"] == "40.00"
    logger.info("冲正成功: reversal_id=%s", body["reversal_id"])

    # 钱全额退回原付款方
    assert client.get("/accounts/payer").json()["balance"] == "100.00"
    assert client.get("/accounts/payee").json()["balance"] == "0.00"

    # 回放：既能还原原始转账，也能看出对应冲正，重建余额对得上
    report = client.get("/replay").json()
    assert report["consistent"]
    # 事件：payer 初始入金 1 + 转账 2 + 冲正 2，原始事件未被删除
    assert report["event_count"] == 5
    assert report["reversals"] == [{
        "transfer_id": transfer_id,
        "reversal_id": body["reversal_id"],
        "amount": "40.00",
    }]
    logger.info("回放一致，冲正对应关系: %s", report["reversals"])

    # 原始转账事件保持原样（reversal_of 为空、金额未变）
    service: LedgerService = client.app.state.service
    with service._store.read_conn() as conn:
        originals = Store.list_events_for_transfer(conn, transfer_id)
    assert len(originals) == 2
    assert all(e.reversal_of is None for e in originals)
    assert {e.kind for e in originals} == {"debit", "credit"}


def test_reversal_duplicate_submission_returns_same_result(client: TestClient):
    transfer_id = _open_and_transfer(client)
    first = client.post(f"/transfers/{transfer_id}/reversal",
                        json={"idempotency_key": "rev-dup"})
    assert first.status_code == 200

    for _ in range(3):
        again = client.post(f"/transfers/{transfer_id}/reversal",
                            json={"idempotency_key": "rev-dup"})
        assert again.status_code == 200
        assert again.json() == first.json(), "重复冲正必须返回与首次一致的结果"
        assert again.headers["x-idempotent-replay"] == "true"

    # 不重复退款：余额只回补一次
    assert client.get("/accounts/payer").json()["balance"] == "100.00"
    assert client.get("/accounts/payee").json()["balance"] == "0.00"
    report = client.get("/replay").json()
    assert report["event_count"] == 5  # 冲正事件只有一对
    assert report["consistent"]
    logger.info("重复提交 3 次冲正，仅首次记账")


def test_reversal_of_nonexistent_transfer_is_distinct_error(client: TestClient):
    r = client.post("/transfers/no-such-transfer/reversal",
                    json={"idempotency_key": "rev-ghost"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "TRANSFER_NOT_FOUND"
    logger.info("不存在的转账 -> TRANSFER_NOT_FOUND")


def test_already_reversed_is_distinct_from_not_found(client: TestClient):
    transfer_id = _open_and_transfer(client)
    assert client.post(f"/transfers/{transfer_id}/reversal",
                       json={"idempotency_key": "rev-first"}).status_code == 200

    # 换提交标识再次冲正同一笔 -> 409，且与“不存在”是可区分的错误码
    r = client.post(f"/transfers/{transfer_id}/reversal",
                    json={"idempotency_key": "rev-second"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "TRANSFER_ALREADY_REVERSED"
    logger.info("重复冲正（换键）-> TRANSFER_ALREADY_REVERSED")

    # 不多退：余额仍只回补一次
    assert client.get("/accounts/payer").json()["balance"] == "100.00"
    assert client.get("/accounts/payee").json()["balance"] == "0.00"
    assert client.get("/replay").json()["consistent"]


def test_reversal_fails_cleanly_when_payee_spent_the_money(client: TestClient):
    """收款方已把钱花掉时，冲正必须失败且不得把余额冲成负数。"""
    transfer_id = _open_and_transfer(client)
    client.post("/accounts", json={"account_id": "sink"})
    # payee 把 40.00 中的 35.00 转走，只剩 5.00，不够全额冲正
    assert client.post("/transfers", json={
        "from_account": "payee", "to_account": "sink",
        "amount": "35.00", "idempotency_key": "spend",
    }).status_code == 200

    r = client.post(f"/transfers/{transfer_id}/reversal",
                    json={"idempotency_key": "rev-late"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INSUFFICIENT_FUNDS"
    logger.info("收款方余额不足，冲正被拒绝: %s", r.json()["error"]["message"])

    # 状态不变、无负数、无残留事件
    assert client.get("/accounts/payer").json()["balance"] == "60.00"
    assert client.get("/accounts/payee").json()["balance"] == "5.00"
    report = client.get("/replay").json()
    assert report["consistent"]
    assert report["reversals"] == []


def test_concurrent_reversals_refund_exactly_once(service: LedgerService):
    """并发压到冲正路径：同键与不同键混杂，最多只冲正一次、不多退。"""
    service.create_account("payer", 10000)
    service.create_account("payee", 0)
    _, body, _ = service.transfer("payer", "payee", 4000, "pay-conc-rev")
    transfer_id = body["transfer_id"]

    n = 16
    keys = ["rev-same"] * (n // 2) + [f"rev-other-{i}" for i in range(n // 2)]
    outcomes: list[tuple[str, object]] = []

    def submit(key: str) -> tuple[str, object]:
        try:
            status, resp, replayed = service.reverse_transfer(transfer_id, key)
            return ("ok-replayed" if replayed else "ok-booked", resp)
        except (TransferAlreadyReversed, TransferNotFound) as exc:
            return (type(exc).__name__, exc.code)

    with ThreadPoolExecutor(max_workers=n) as pool:
        outcomes = list(pool.map(submit, keys))

    booked = [o for o in outcomes if o[0] == "ok-booked"]
    replayed = [o for o in outcomes if o[0] == "ok-replayed"]
    already = [o for o in outcomes if o[0] == "TransferAlreadyReversed"]
    logger.info("并发 %d 次冲正: 记账 %d, 幂等重放 %d, 已冲正拒绝 %d",
                n, len(booked), len(replayed), len(already))

    # 恰好一次真正记账；同键的其余请求重放，不同键的被可区分地拒绝
    assert len(booked) == 1
    assert len(replayed) == n // 2 - 1
    assert len(already) == n // 2
    # 所有成功响应的 reversal_id 一致
    reversal_ids = {o[1]["reversal_id"] for o in booked + replayed}
    assert len(reversal_ids) == 1

    # 只退一次：余额精确回补，不多退、不负数
    assert service.get_account("payer").balance_cents == 10000
    assert service.get_account("payee").balance_cents == 0
    report = service.replay()
    assert report["consistent"]
    assert report["event_count"] == 1 + 2 + 2  # 初始入金 + 转账 + 冲正
    logger.info("并发冲正后余额正确，回放一致")


def test_reversal_preserves_total_balance_conservation(service: LedgerService):
    """转账 + 冲正后系统总余额不变（守恒不被冲正破坏）。"""
    service.create_account("a", 7000)
    service.create_account("b", 3000)
    total = 10000
    _, body, _ = service.transfer("a", "b", 2500, "cons-pay")
    service.reverse_transfer(body["transfer_id"], "cons-rev")
    assert (service.get_account("a").balance_cents
            + service.get_account("b").balance_cents) == total
    assert service.replay()["consistent"]


def test_failed_reversal_leaves_no_partial_state(service: LedgerService):
    """冲正失败（已冲正过）不产生任何新事件或幂等记录残留。"""
    service.create_account("a", 1000)
    service.create_account("b", 0)
    _, body, _ = service.transfer("a", "b", 500, "fp-pay")
    service.reverse_transfer(body["transfer_id"], "fp-rev")

    with pytest.raises(TransferAlreadyReversed):
        service.reverse_transfer(body["transfer_id"], "fp-rev-again")

    with service._store.read_conn() as conn:
        events = Store.list_events(conn)
        idem = Store.get_idempotency(
            conn, f"reversal:{body['transfer_id']}", "fp-rev-again")
    assert len(events) == 1 + 2 + 2  # 初始入金 + 转账 + 首次冲正，无新增
    assert idem is None
    assert service.replay()["consistent"]
    logger.info("失败的二次冲正无残留，状态一致")
