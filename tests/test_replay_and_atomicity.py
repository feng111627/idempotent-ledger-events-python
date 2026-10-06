"""业务场景：事件回放一致性 + 失败时的原子性（事件与余额不落单边）。"""

import logging
import sqlite3

import pytest

from ledger.errors import InsufficientFunds, StorageError
from ledger.models import format_cents
from ledger.service import LedgerService
from ledger.store import Store

logger = logging.getLogger("ledger.tests.replay")


def _mixed_activity(service: LedgerService) -> None:
    service.create_account("a", 100000)
    service.create_account("b", 5000)
    service.create_account("c", 0)
    service.transfer("a", "b", 12345, "t1")
    service.transfer("b", "c", 5000, "t2")
    service.transfer("a", "c", 1, "t3")  # 最小单位 0.01
    service.transfer("a", "b", 99999 - 12345, "t4")


def test_replay_rebuilds_balances(service: LedgerService):
    _mixed_activity(service)
    report = service.replay()
    logger.info("全量回放: %d 条事件, consistent=%s",
                report["event_count"], report["consistent"])
    assert report["consistent"]
    for acc in report["accounts"]:
        assert acc["match"], acc
        logger.info("账户 %s: stored=%s rebuilt=%s",
                    acc["account_id"], acc["stored_balance"],
                    acc["rebuilt_balance"])


def test_replay_single_account(service: LedgerService):
    _mixed_activity(service)
    report = service.replay("a")
    assert report["consistent"]
    assert report["accounts"][0]["account_id"] == "a"
    assert report["accounts"][0]["rebuilt_balance"] == \
        format_cents(service.get_account("a").balance_cents)


def test_event_chain_balance_after_is_verifiable(service: LedgerService):
    """每条事件记录的 balance_after 必须等于重放运行余额（链式可验证）。"""
    _mixed_activity(service)
    with service._store.read_conn() as conn:
        events = Store.list_events(conn)
    running: dict[str, int] = {}
    for ev in events:
        running[ev.account_id] = running.get(ev.account_id, 0) + ev.delta_cents
        assert ev.balance_after_cents == running[ev.account_id]
        # 借贷符号约定
        assert ev.delta_cents < 0 if ev.kind == "debit" else ev.delta_cents > 0
    logger.info("校验 %d 条事件的 balance_after 链式一致", len(events))


def test_failed_transfer_leaves_no_partial_state(service: LedgerService):
    """余额不足的转账：余额不变、无事件、无幂等记录（不落单边）。"""
    service.create_account("a", 1000)
    service.create_account("b", 0)
    with pytest.raises(InsufficientFunds):
        service.transfer("a", "b", 1001, "fail-1")

    assert service.get_account("a").balance_cents == 1000
    assert service.get_account("b").balance_cents == 0
    with service._store.read_conn() as conn:
        events = Store.list_events(conn)
        idem = Store.get_idempotency(conn, "fail-1")
    # 只有 a 的初始入金事件，失败转账不产生任何事件/幂等记录
    assert len(events) == 1
    assert idem is None
    assert service.replay()["consistent"]
    logger.info("失败转账后无残留事件与幂等记录，状态一致")


def test_mid_transaction_failure_rolls_back(service: LedgerService):
    """模拟写入中途异常：整个事务回滚，事件与余额不出现单边。"""
    service.create_account("a", 1000)
    service.create_account("b", 0)

    original = Store.insert_event
    calls = {"n": 0}

    def failing_insert(conn, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:  # 第二条事件写入时崩溃
            raise sqlite3.OperationalError("simulated disk failure")
        return original(conn, *args, **kwargs)

    Store.insert_event = staticmethod(failing_insert)
    try:
        with pytest.raises(StorageError) as exc_info:
            service.transfer("a", "b", 500, "atomic-1")
        # 驱动原始报错不得泄露
        assert "simulated disk failure" not in str(exc_info.value.message)
        assert "OperationalError" in str(exc_info.value.message)
    finally:
        Store.insert_event = staticmethod(original)

    assert service.get_account("a").balance_cents == 1000
    assert service.get_account("b").balance_cents == 0
    with service._store.read_conn() as conn:
        assert len(Store.list_events(conn)) == 1  # 仅初始入金
        assert Store.get_idempotency(conn, "atomic-1") is None
    assert service.replay()["consistent"]
    logger.info("中途异常已回滚：余额与事件保持一致，异常被归一化为 StorageError")


def test_storage_error_normalization(service: LedgerService):
    """底层 sqlite 异常一律归一化为 StorageError，不暴露驱动细节。"""
    service.create_account("a", 1000)
    service.create_account("b", 0)

    original = Store.get_account

    def broken(conn, account_id):
        raise sqlite3.DatabaseError("database disk image is malformed")

    Store.get_account = staticmethod(broken)
    try:
        with pytest.raises(StorageError) as exc_info:
            service.transfer("a", "b", 100, "norm-1")
        assert "malformed" not in exc_info.value.message
        assert exc_info.value.code == "STORAGE_ERROR"
    finally:
        Store.get_account = staticmethod(original)
    logger.info("驱动异常已归一化: code=%s", StorageError.code)
