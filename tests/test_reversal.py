"""业务场景：全额冲正 / 退款。

- 冲正以追加事件落库，原转账历史不改不删；
- 一笔转账最多冲正一次，重复 / 并发冲正不会重复退款、多退或冲成负数；
- 冲正按提交标识幂等；
- 转账不存在与已被冲正返回彼此可区分的失败原因。
"""

import logging

import pytest
from fastapi.testclient import TestClient

from ledger.errors import TransferAlreadyReversed, TransferNotFound
from ledger.service import LedgerService
from ledger.store import Store

logger = logging.getLogger("ledger.tests.reversal")


def _make_transfer(service: LedgerService) -> tuple[str, str, str, int]:
    service.create_account("a", 10000)
    service.create_account("b", 0)
    _, body, _ = service.transfer("a", "b", 3000, "pay-1")
    return body["transfer_id"], "a", "b", 3000


def test_reversal_refunds_full_amount_appends_events(service: LedgerService):
    transfer_id, payer, payee, amount = _make_transfer(service)

    status, body, replayed = service.reverse_transfer(transfer_id, "rev-1")
    assert status == 200
    assert replayed is False
    assert body["transfer_id"] == transfer_id
    assert body["reversal_id"]
    assert body["refund_to_account"] == payer
    assert body["debit_account"] == payee
    assert body["amount"] == "30.00"
    logger.info("冲正成功: %s", body)

    # 钱全额退回付款方
    assert service.get_account(payer).balance_cents == 10000
    assert service.get_account(payee).balance_cents == 0

    # 事件流：原始一借一贷仍在，冲正再追加两条（加 a 的初始入金共 5 条）
    report = service.replay()
    assert report["consistent"], report
    assert report["event_count"] == 5
    transfer_summary = next(
        t for t in report["transfers"] if t["transfer_id"] == transfer_id
    )
    assert transfer_summary["amount"] == "30.00"
    assert transfer_summary["reversed"] is True
    assert len(report["reversals"]) == 1
    assert report["reversals"][0]["transfer_id"] == transfer_id
    logger.info("回放可见原转账与冲正: transfer=%s", transfer_summary)


def test_reversal_idempotent_repeat_returns_same_result(service: LedgerService):
    transfer_id, _, _, _ = _make_transfer(service)

    first = service.reverse_transfer(transfer_id, "rev-key")
    for _ in range(3):
        again = service.reverse_transfer(transfer_id, "rev-key")
        assert again[0] == first[0]
        assert again[1] == first[1], "重复冲正必须返回与首次一致的结果"
        assert again[2] is True

    assert service.get_account("a").balance_cents == 10000
    assert service.get_account("b").balance_cents == 0
    report = service.replay()
    # 只有一次冲正的两条追加事件
    assert len(report["reversals"]) == 1
    assert report["event_count"] == 5
    logger.info("重复提交冲正 3 次：结果一致且只记账一次")


def test_reverse_unknown_transfer_is_not_found(service: LedgerService):
    service.create_account("a", 10000)
    with pytest.raises(TransferNotFound):
        service.reverse_transfer("no-such-transfer", "rev-x")
    logger.info("冲正不存在的转账 -> TRANSFER_NOT_FOUND")


def test_reverse_already_reversed_transfer_is_distinct_error(service: LedgerService):
    transfer_id, _, _, _ = _make_transfer(service)
    service.reverse_transfer(transfer_id, "rev-first")

    # 换一个新的提交标识再冲一次：不是幂等重放，而是“已冲正”
    with pytest.raises(TransferAlreadyReversed):
        service.reverse_transfer(transfer_id, "rev-second")
    logger.info("重复冲正已冲正转账 -> TRANSFER_ALREADY_REVERSED")


def test_reversal_keeps_balance_nonnegative(service: LedgerService):
    """收款方已把钱转走、余额不足时，冲正不得把余额冲成负数。"""
    service.create_account("a", 10000)
    service.create_account("b", 0)
    service.create_account("c", 0)
    _, body, _ = service.transfer("a", "b", 3000, "pay-1")
    transfer_id = body["transfer_id"]
    # b 把钱再转给 c，b 余额回到 0
    service.transfer("b", "c", 3000, "pay-2")

    with pytest.raises(Exception) as exc_info:
        service.reverse_transfer(transfer_id, "rev-1")
    assert exc_info.value.code == "INSUFFICIENT_FUNDS"
    assert service.get_account("b").balance_cents == 0
    assert service.get_account("a").balance_cents == 7000
    assert len(service.replay()["reversals"]) == 0
    assert service.replay()["consistent"]
    logger.info("余额不足的冲正被拒绝，无账户被冲成负数")


def test_reversal_unique_constraint_violation_is_business_error(
    service: LedgerService,
):
    """第二道防线：绕过前置复查直接撞 reversals 唯一约束，
    也必须归一化为 TRANSFER_ALREADY_REVERSED，而不是 500。"""
    service.create_account("a", 10000)
    service.create_account("b", 10000)
    _, body, _ = service.transfer("a", "b", 3000, "pay-x")
    transfer_id = body["transfer_id"]
    service.reverse_transfer(transfer_id, "rev-first")
    # 冲正后 a=10000、b=10000；b 仍有足够余额，使第二次流程能走到落库

    original = Store.get_reversal_by_transfer

    def blind(conn, _tid):
        # 模拟前置复查在极端竞态下没看到已存在冲正的情况
        return None

    Store.get_reversal_by_transfer = staticmethod(blind)
    try:
        with pytest.raises(TransferAlreadyReversed):
            service.reverse_transfer(transfer_id, "rev-second")
    finally:
        Store.get_reversal_by_transfer = staticmethod(original)

    assert service.get_account("a").balance_cents == 10000
    assert service.get_account("b").balance_cents == 10000
    assert len(service.replay()["reversals"]) == 1
    logger.info("唯一约束冲突已归一化为 TRANSFER_ALREADY_REVERSED")


def test_reversal_api_happy_path_and_replay_header(client: TestClient):
    client.post("/accounts", json={"account_id": "a", "initial_balance": "100.00"})
    client.post("/accounts", json={"account_id": "b"})
    paid = client.post("/transfers", json={
        "from_account": "a", "to_account": "b",
        "amount": "30.00", "idempotency_key": "pay-1",
    }).json()

    r1 = client.post(
        f"/transfers/{paid['transfer_id']}/reversal",
        json={"idempotency_key": "rev-1"},
    )
    assert r1.status_code == 200
    assert "x-idempotent-replay" not in r1.headers
    r2 = client.post(
        f"/transfers/{paid['transfer_id']}/reversal",
        json={"idempotency_key": "rev-1"},
    )
    assert r2.status_code == 200
    assert r2.headers["x-idempotent-replay"] == "true"
    assert r2.json() == r1.json()

    assert client.get("/accounts/a").json()["balance"] == "100.00"
    assert client.get("/accounts/b").json()["balance"] == "0.00"


def test_reversal_api_distinct_error_codes(client: TestClient):
    r = client.post(
        "/transfers/missing/reversal", json={"idempotency_key": "rev-x"}
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "TRANSFER_NOT_FOUND"

    client.post("/accounts", json={"account_id": "a", "initial_balance": "10.00"})
    client.post("/accounts", json={"account_id": "b"})
    paid = client.post("/transfers", json={
        "from_account": "a", "to_account": "b",
        "amount": "4.00", "idempotency_key": "pay-1",
    }).json()
    assert client.post(
        f"/transfers/{paid['transfer_id']}/reversal",
        json={"idempotency_key": "rev-1"},
    ).status_code == 200
    r2 = client.post(
        f"/transfers/{paid['transfer_id']}/reversal",
        json={"idempotency_key": "rev-2"},
    )
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "TRANSFER_ALREADY_REVERSED"
    logger.info("冲正两类失败原因可区分: NOT_FOUND vs ALREADY_REVERSED")
