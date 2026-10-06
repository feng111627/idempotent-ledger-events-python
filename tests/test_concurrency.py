"""业务：并发转账一致性。

场景都通过多线程 + 每线程独立连接发起，验证：
1. 双向密集转账：无透支、无丢更新、总额守恒、事件余额链自洽；
2. 单账户热点竞争：全部成功时总入账精确等于请求总额；
3. 超额并发抢兑：成功者数受余额约束，账户绝不透支；
4. 同一幂等键并发提交：恰好记账一次。
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from app.db import Database
from app.errors import IdempotencyConflict, InsufficientFunds, LedgerError
from app.repository import LedgerRepository
from app.service import LedgerService

log = logging.getLogger("ledger.test.concurrent")


def _service(database: Database) -> LedgerService:
    """每个线程通过同一 Database 拿到自己的连接。"""
    return LedgerService(LedgerRepository(database))


def _totals(service: LedgerService) -> tuple[dict[str, int], int]:
    balances = {}
    total = 0
    # replay 内部已校验事件链自洽且与账户表一致
    rep = service.replay()
    for a in rep["accounts"]:
        balances[a["account_id"]] = a["replayed_balance_minor"]
        total += a["replayed_balance_minor"]
    return balances, total


class TestConcurrentTransfers:
    def test_two_accounts_intensive_transfers_conserve_total(self, database):
        svc = _service(database)
        svc.create_account("A", 1_000_000, "open-A")  # 10000.00
        svc.create_account("B", 1_000_000, "open-B")
        INITIAL = 2_000_000

        # 每个方向 100 笔、每笔 13.00；两边初始余额都足够覆盖最坏单侧
        plan: list[tuple[str, str, int, str]] = []
        for i in range(100):
            plan.append(("A", "B", 1300, f"ab-{i}"))
            plan.append(("B", "A", 700, f"ba-{i}"))

        gate = threading.Event()

        def worker(job):
            frm, to, amt, key = job
            s = _service(database)
            gate.wait()  # 所有任务就位后统一放行，制造真正竞争
            s.transfer(frm, to, amt, key)
            return key

        with ThreadPoolExecutor(max_workers=16) as pool:
            futures = [pool.submit(worker, j) for j in plan]
            gate.set()
            done = [f.result() for f in as_completed(futures)]
        assert len(done) == len(plan)

        bal, total = _totals(svc)
        log.info("[密集双向转账] 200 笔全部成功，A=%d B=%d 总额=%d（初始 %d）",
                 bal["A"], bal["B"], total, INITIAL)
        assert total == INITIAL, "总余额不守恒"
        # A: -1300*100 +700*100 = -60000；B 反之
        assert bal["A"] == 1_000_000 - 60_000
        assert bal["B"] == 1_000_000 + 60_000
        for acct, value in bal.items():
            assert value >= 0, f"{acct} 透支"

        # 事件链：每条事件的 balance_after 与重放一致（replay 内部已校验）
        events = svc.list_events()
        assert len(events) == 2 + 2 * len(plan)

    def test_hot_account_inbound_is_never_lost(self, database):
        svc = _service(database)
        svc.create_account("HUB", 0, "open-hub")
        for i in range(20):
            svc.create_account(f"S{i:02d}", 10_000, f"open-{i}")

        jobs = [(i, f"S{i:02d}", "HUB", 333) for i in range(20)]
        gate = threading.Event()

        def worker(job):
            i, frm, to, amt = job
            s = _service(database)
            gate.wait()
            s.transfer(frm, to, amt, f"in-{i}")

        with ThreadPoolExecutor(max_workers=20) as pool:
            futures = [pool.submit(worker, j) for j in jobs]
            gate.set()
            [f.result() for f in as_completed(futures)]

        hub = svc.get_balance("HUB")["balance_minor"]
        log.info("[热点入账] 20 线程各转 3.33，HUB 余额=%d（期望 6660）", hub)
        assert hub == 20 * 333, "存在丢更新"
        _, total = _totals(svc)
        assert total == 20 * 10_000

    def test_oversubscription_never_overdraws(self, database):
        svc = _service(database)
        svc.create_account("RICH", 5_000, "open-rich")   # 50.00
        for i in range(10):
            svc.create_account(f"C{i}", 0, f"open-{i}")

        # 10 个线程同时想从 RICH 转走 20.00，只有 2 笔能成功
        gate = threading.Event()
        outcomes: list[str] = []
        lock = threading.Lock()

        def worker(i):
            s = _service(database)
            gate.wait()
            try:
                s.transfer("RICH", f"C{i}", 2_000, f"grab-{i}")
                with lock:
                    outcomes.append("ok")
            except InsufficientFunds:
                with lock:
                    outcomes.append("short")
            except LedgerError as e:
                with lock:
                    outcomes.append(f"other:{e.code}")

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = [pool.submit(worker, i) for i in range(10)]
            gate.set()
            [f.result() for f in as_completed(futures)]

        ok = outcomes.count("ok")
        short = outcomes.count("short")
        log.info("[超额抢兑] 成功=%d 因余额不足失败=%d（期望成功 2）", ok, short)
        assert ok == 2, outcomes
        assert ok + short == 10, "出现了非预期的错误分类"
        rich = svc.get_balance("RICH")["balance_minor"]
        assert rich == 1_000, rich  # 50 - 40 = 10，绝不为负
        _, total = _totals(svc)
        assert total == 5_000

    def test_concurrent_same_idempotency_key_applies_once(self, database):
        svc = _service(database)
        svc.create_account("A", 100_000, "open-A")
        svc.create_account("B", 0, "open-B")

        n = 12
        gate = threading.Event()
        outcomes: list[str] = []
        lock = threading.Lock()

        def worker(_):
            s = _service(database)
            gate.wait()
            try:
                res = s.transfer("A", "B", 1_000, "SAME-KEY")
                # 首次落账 replayed=False；并发输掉竞争的线程拿到首次
                # 结果的回放 replayed=True，二者都不应重复记账
                with lock:
                    outcomes.append("booked" if not res.get("replayed")
                                    else "replayed")
            except IdempotencyConflict:
                with lock:
                    outcomes.append("conflict")

        with ThreadPoolExecutor(max_workers=n) as pool:
            futures = [pool.submit(worker, i) for i in range(n)]
            gate.set()
            [f.result() for f in as_completed(futures)]

        # BEGIN IMMEDIATE 串行化：恰好 1 个线程首次落账，其余全部拿到
        # 与首次一致的回放结果，且没有任何一个报冲突
        log.info("[同键并发] 首次落账=%d 回放=%d 冲突=%d，A=%s B=%s",
                 outcomes.count("booked"), outcomes.count("replayed"),
                 outcomes.count("conflict"),
                 svc.get_balance("A")["balance"],
                 svc.get_balance("B")["balance"])
        assert outcomes.count("booked") == 1, outcomes
        assert outcomes.count("replayed") == n - 1, outcomes
        assert outcomes.count("conflict") == 0, outcomes
        assert svc.get_balance("B")["balance_minor"] == 1_000
        _, total = _totals(svc)
        assert total == 100_000
