"""业务：追加事件流回放一致性。

- 事件按 seq 重放可重建余额，且与账户表一致；
- 总额守恒（开户总额不变，转账只是搬移）；
- 中途失败的转账不留任何事件（原子性）；
- 持久化：重启（重新连接同一库文件）后事件与余额可复现。
"""
from __future__ import annotations

import logging

from app import db
from app.repository import LedgerRepository
from app.service import LedgerService

log = logging.getLogger("ledger.test.replay")


class TestEventReplay:
    def test_replay_rebuilds_balances_and_conserves_total(self, database):
        svc = LedgerService(LedgerRepository(database))
        svc.create_account("A", 100_000, "ka")
        svc.create_account("B", 50_000, "kb")
        svc.create_account("C", 0, "kc")
        svc.transfer("A", "B", 10_000, "t1")
        svc.transfer("B", "C", 5_000, "t2")
        svc.transfer("A", "C", 2_500, "t3")

        rep = svc.replay()
        log.info("[回放] 事件=%d 总额=%s 明细=%s",
                 rep["events_replayed"], rep["total_balance"],
                 {a["account_id"]: a["replayed_balance_minor"]
                  for a in rep["accounts"]})
        assert rep["events_replayed"] == 9   # 3 OPEN + 3*2 transfer
        assert rep["total_balance_minor"] == 150_000  # 总额守恒
        by = {a["account_id"]: a["replayed_balance_minor"]
              for a in rep["accounts"]}
        assert by == {"A": 87_500, "B": 55_000, "C": 7_500}
        assert all(a["matches"] for a in rep["accounts"])

    def test_event_balance_chain_is_self_consistent(self, database):
        """每条事件的 balance_after 必须等于按序重算值（服务回放已校验）。

        这里直接按事件流独立重算一遍，双重确认。
        """
        svc = LedgerService(LedgerRepository(database))
        svc.create_account("A", 1_000, "ka")
        svc.create_account("B", 0, "kb")
        for i in range(5):
            svc.transfer("A", "B", 100, f"t{i}")

        running: dict[str, int] = {}
        for e in svc.list_events():
            sign = 1 if e["event_type"] in ("open", "transfer_in") else -1
            running[e["account_id"]] = running.get(e["account_id"], 0) + \
                sign * e["amount_minor"]
            assert running[e["account_id"]] == e["balance_after_minor"], e
        log.info("[事件链] 6 个事件后独立重算与事件快照逐条一致：%s", running)
        assert running == {"A": 500, "B": 500}

    def test_failed_transfer_appends_no_events(self, database):
        """透支转账整事务回滚：余额与事件都不变（绝不只落一边）。"""
        svc = LedgerService(LedgerRepository(database))
        svc.create_account("A", 1_000, "ka")
        svc.create_account("B", 0, "kb")
        before = len(svc.list_events())

        from app.errors import InsufficientFunds
        try:
            svc.transfer("A", "B", 2_000, "bad")
            raise AssertionError("应透支")
        except InsufficientFunds:
            pass

        after = len(svc.list_events())
        assert before == after == 2, (before, after)
        assert svc.get_balance("A")["balance_minor"] == 1_000
        assert svc.get_balance("B")["balance_minor"] == 0
        # 回放开销下依然守恒
        assert svc.replay()["total_balance_minor"] == 1_000
        log.info("[原子性] 透支回滚后事件数不变(%d)，总额仍为 10.00", after)

    def test_replay_single_account(self, database):
        svc = LedgerService(LedgerRepository(database))
        svc.create_account("A", 1_000, "ka")
        svc.create_account("B", 1_000, "kb")
        svc.transfer("A", "B", 300, "t1")

        rep = svc.replay("A")
        assert len(rep["accounts"]) == 1
        assert rep["accounts"][0]["replayed_balance_minor"] == 700
        log.info("[单账户回放] A 重放余额=7.00，与账户表一致")

    def test_replay_persists_across_restart(self, tmp_path):
        """关闭并重新连接同一库文件，事件与余额必须原样可复现。"""
        path = tmp_path / "persist.db"
        d1 = db.Database(path)
        d1.init()
        svc1 = LedgerService(LedgerRepository(d1))
        svc1.create_account("A", 5_000, "ka")
        svc1.create_account("B", 5_000, "kb")
        svc1.transfer("A", "B", 1_200, "t1")
        d1.close_current()

        # 模拟进程重启：全新 Database/连接
        d2 = db.Database(path)
        svc2 = LedgerService(LedgerRepository(d2))
        rep = svc2.replay()
        assert rep["total_balance_minor"] == 10_000
        assert svc2.get_balance("A")["balance_minor"] == 3_800
        assert svc2.get_balance("B")["balance_minor"] == 6_200
        assert rep["events_replayed"] == 4
        log.info("[持久化重启] 重连后重放：A=38.00 B=62.00 总额守恒，"
                 "事件数=%d", rep["events_replayed"])
        d2.close_current()
