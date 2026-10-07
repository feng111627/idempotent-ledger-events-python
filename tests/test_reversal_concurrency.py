"""业务场景：冲正的并发与重复提交 —— 真正走多线程并发路径。"""

import logging
from concurrent.futures import ThreadPoolExecutor

from ledger.errors import TransferAlreadyReversed
from ledger.service import LedgerService

logger = logging.getLogger("ledger.tests.reversal_concurrency")


def test_concurrent_duplicate_reversal_with_same_key_refunds_once(
    service: LedgerService,
):
    service.create_account("a", 10000)
    service.create_account("b", 0)
    _, body, _ = service.transfer("a", "b", 3000, "pay-1")
    transfer_id = body["transfer_id"]
    n = 16

    def submit(_: int):
        return service.reverse_transfer(transfer_id, "rev-shared")

    with ThreadPoolExecutor(max_workers=n) as pool:
        results = list(pool.map(submit, range(n)))

    success = [r for r in results if not r[2]]
    replays = [r for r in results if r[2]]
    assert len(success) == 1, "并发同键冲正只能有一次真正记账"
    assert len(replays) == n - 1
    assert {r[1]["reversal_id"] for r in results} == {
        success[0][1]["reversal_id"]
    }
    logger.info("并发 %d 次同键冲正：1 次记账，%d 次幂等重放", n, len(replays))

    # 不重复退款、不多退
    assert service.get_account("a").balance_cents == 10000
    assert service.get_account("b").balance_cents == 0
    report = service.replay()
    assert len(report["reversals"]) == 1
    assert report["event_count"] == 5
    assert report["consistent"]


def test_concurrent_reversal_with_distinct_keys_only_one_wins(
    service: LedgerService,
):
    service.create_account("a", 10000)
    service.create_account("b", 0)
    _, body, _ = service.transfer("a", "b", 3000, "pay-1")
    transfer_id = body["transfer_id"]
    n = 16

    def submit(i: int):
        try:
            return service.reverse_transfer(transfer_id, f"rev-{i}")
        except TransferAlreadyReversed as exc:
            return exc

    with ThreadPoolExecutor(max_workers=n) as pool:
        results = list(pool.map(submit, range(n)))

    won = [r for r in results if not isinstance(r, TransferAlreadyReversed)]
    rejected = [r for r in results if isinstance(r, TransferAlreadyReversed)]
    assert len(won) == 1, "不同 key 并发冲正同一笔转账，只能有一个成功"
    assert len(rejected) == n - 1
    logger.info("并发 %d 次不同键冲正：1 成功，%d 次 ALREADY_REVERSED",
                n, len(rejected))

    assert service.get_account("a").balance_cents == 10000
    assert service.get_account("b").balance_cents == 0
    report = service.replay()
    assert len(report["reversals"]) == 1
    assert report["consistent"]


def test_concurrent_reversals_repeated_rounds(service: LedgerService):
    """多轮并发冲正确认无竞态窗口（锁调度可能因轮次而异）。"""
    rounds = 10
    n = 8
    for r in range(rounds):
        service.create_account(f"a{r}", 10000)
        service.create_account(f"b{r}", 0)
        _, body, _ = service.transfer(
            f"a{r}", f"b{r}", 1000, f"pay-{r}"
        )
        tid = body["transfer_id"]

        def submit(i: int, _tid=tid, _r=r):
            try:
                return service.reverse_transfer(_tid, f"rev-{_r}-{i}")
            except TransferAlreadyReversed as exc:
                return exc

        with ThreadPoolExecutor(max_workers=n) as pool:
            results = list(pool.map(submit, range(n)))
        won = [x for x in results if not isinstance(x, TransferAlreadyReversed)]
        assert len(won) == 1

    total = service.replay()
    assert total["consistent"]
    assert len(total["reversals"]) == rounds
    # 每轮都全额退回：每对账户恢复成 a=10000, b=0
    for r in range(rounds):
        assert service.get_account(f"a{r}").balance_cents == 10000
        assert service.get_account(f"b{r}").balance_cents == 0
    logger.info("%d 轮并发冲正全部只成功一次，余额守恒", rounds)
