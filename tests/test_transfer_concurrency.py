"""业务场景：并发转账 —— 不透支、不丢更新、总余额守恒。"""

import logging
from concurrent.futures import ThreadPoolExecutor

from ledger.service import LedgerService

logger = logging.getLogger("ledger.tests.concurrency")


def test_concurrent_transfers_no_overdraft_and_conservation(service: LedgerService):
    n_threads = 16
    per_thread = 25
    amount_cents = 100  # 每笔 1.00
    initial = n_threads * per_thread * amount_cents // 2  # 只够一半成功

    service.create_account("src", initial)
    service.create_account("dst", 0)
    logger.info("并发场景: %d 线程 x %d 笔, 每笔 %d 分, 初始余额 %d 分",
                n_threads, per_thread, amount_cents, initial)

    def worker(tid: int) -> int:
        ok = 0
        for i in range(per_thread):
            try:
                service.transfer("src", "dst", amount_cents, f"conc-{tid}-{i}")
                ok += 1
            except Exception as exc:
                assert type(exc).__name__ == "InsufficientFunds", exc
        return ok

    with ThreadPoolExecutor(max_workers=n_threads) as pool:
        results = list(pool.map(worker, range(n_threads)))

    succeeded = sum(results)
    src = service.get_account("src")
    dst = service.get_account("dst")
    logger.info("成功 %d 笔, src=%d, dst=%d", succeeded,
                src.balance_cents, dst.balance_cents)

    # 不透支
    assert src.balance_cents >= 0
    # 不丢更新：成功笔数与余额变动一致
    assert succeeded * amount_cents == initial - src.balance_cents
    assert dst.balance_cents == succeeded * amount_cents
    # 总余额守恒
    assert src.balance_cents + dst.balance_cents == initial
    # 回放一致
    report = service.replay()
    assert report["consistent"], report


def test_concurrent_bidirectional_transfers_conservation(service: LedgerService):
    """双向并发转账：总额始终守恒，回放可重建。"""
    service.create_account("a", 50000)
    service.create_account("b", 50000)
    total_before = 100000
    n = 10
    rounds = 20

    def worker(tid: int) -> None:
        for i in range(rounds):
            frm, to = ("a", "b") if i % 2 == 0 else ("b", "a")
            service.transfer(frm, to, 10, f"bidi-{tid}-{i}")

    with ThreadPoolExecutor(max_workers=n) as pool:
        list(pool.map(worker, range(n)))

    a = service.get_account("a")
    b = service.get_account("b")
    logger.info("双向转账后: a=%d, b=%d", a.balance_cents, b.balance_cents)
    assert a.balance_cents + b.balance_cents == total_before
    report = service.replay()
    assert report["consistent"], report
    # 转账事件 n*rounds*2 条 + 两个账户的初始入金事件各 1 条
    assert report["event_count"] == n * rounds * 2 + 2
