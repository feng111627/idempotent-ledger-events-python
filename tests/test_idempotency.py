"""业务：幂等提交语义。

- 同键同内容的重复请求：返回与首次一致的结果、不重复记账；
- 同键换内容：区分化失败 idempotency_conflict，不静默覆盖；
- 同键跨操作类型：同样拒绝；
- 首次请求失败（透支）：不留下幂等记录，键可被后续合法请求复用。
"""
from __future__ import annotations

import logging

log = logging.getLogger("ledger.test.idem")


def _open(client, acct_id, opening, key):
    return client.post("/accounts", json={
        "account_id": acct_id, "opening_balance": opening,
        "idempotency_key": key})


def _transfer(client, frm, to, amount, key):
    return client.post("/transfers", json={
        "from_account_id": frm, "to_account_id": to,
        "amount": amount, "idempotency_key": key})


class TestTransferIdempotency:
    def test_duplicate_returns_first_result_without_double_booking(self, client):
        _open(client, "A", "100.00", "ka")
        _open(client, "B", "0.00", "kb")

        first = _transfer(client, "A", "B", "30.00", "tx-1")
        assert first.status_code == 201
        first_body = first.json()
        assert first_body["replayed"] is False

        second = _transfer(client, "A", "B", "30.00", "tx-1")
        assert second.status_code == 201
        body = second.json()
        log.info("[重复转账] 第二次返回 replayed=%s，金额=%s",
                 body["replayed"], body["amount"])
        assert body["replayed"] is True
        # 除 replayed 标记外，与首次结果一致
        assert {k: v for k, v in body.items() if k != "replayed"} == \
               {k: v for k, v in first_body.items() if k != "replayed"}

        # 关键：只记一次账（A 只少 30，不是 60）
        assert client.get("/accounts/A").json()["balance"] == "70.00"
        assert client.get("/accounts/B").json()["balance"] == "30.00"
        events = client.get("/events").json()
        transfers = [e for e in events["events"]
                     if e["event_type"].startswith("transfer_")]
        assert len(transfers) == 2  # 一次转账 = out + in 两条
        log.info("[不重复记账] 事件总数=%d（其中转账事件=%d），余额 A=70 B=30",
                 events["count"], len(transfers))

    def test_same_key_different_amount_rejected(self, client):
        _open(client, "A", "100.00", "ka")
        _open(client, "B", "0.00", "kb")
        assert _transfer(client, "A", "B", "10.00", "k").status_code == 201

        r = _transfer(client, "A", "B", "20.00", "k")
        log.info("[同键换金额] 状态码=%s code=%s",
                 r.status_code, r.json()["error"]["code"])
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "idempotency_conflict"
        # 余额仍是首次结果
        assert client.get("/accounts/A").json()["balance"] == "90.00"

    def test_same_key_different_direction_rejected(self, client):
        _open(client, "A", "100.00", "ka")
        _open(client, "B", "10.00", "kb")
        assert _transfer(client, "A", "B", "5.00", "k").status_code == 201

        r = _transfer(client, "B", "A", "5.00", "k")
        log.info("[同键换方向] 状态码=%s code=%s",
                 r.status_code, r.json()["error"]["code"])
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "idempotency_conflict"
        assert client.get("/accounts/A").json()["balance"] == "95.00"
        assert client.get("/accounts/B").json()["balance"] == "15.00"

    def test_same_key_across_operations_rejected(self, client):
        _open(client, "A", "100.00", "shared")
        _open(client, "B", "0.00", "kb")
        r2 = _transfer(client, "A", "B", "1.00", "shared")
        log.info("[同键跨操作] 用开户键发起转账 code=%s",
                 r2.json()["error"]["code"])
        assert r2.status_code == 409
        assert r2.json()["error"]["code"] == "idempotency_conflict"
        # 没有发生任何转账
        assert client.get("/accounts/A").json()["balance"] == "100.00"


class TestFailedFirstAttempt:
    def test_failed_transfer_leaves_no_idempotency_record(self, client):
        _open(client, "A", "10.00", "ka")
        _open(client, "B", "0.00", "kb")

        # 首次因透支失败：事务整体回滚，幂等记录也不落库
        r = _transfer(client, "A", "B", "50.00", "retry-key")
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "insufficient_funds"
        log.info("[首次透支] code=%s，事务回滚，幂等键应可复用",
                 r.json()["error"]["code"])

        # 键可被随后合法请求复用，且不会被误判为冲突
        r2 = _transfer(client, "A", "B", "5.00", "retry-key")
        assert r2.status_code == 201, r2.text
        assert r2.json()["replayed"] is False
        assert client.get("/accounts/A").json()["balance"] == "5.00"
        log.info("[失败后复用] 同键合法请求成功，A 余额=5.00")


class TestCreateIdempotency:
    def test_duplicate_open_returns_first_result(self, client):
        first = _open(client, "A", "42.00", "open")
        second = _open(client, "A", "42.00", "open")
        assert first.status_code == 201 and second.status_code == 201
        assert second.json()["replayed"] is True
        assert second.json()["balance"] == "42.00"
        # 只有一个账户、一条 OPEN 事件
        ev = client.get("/events", params={"account_id": "A"}).json()
        assert ev["count"] == 1
        log.info("[重复开户] 复用首次结果，OPEN 事件仅 1 条")

    def test_open_same_key_changed_payload_rejected(self, client):
        _open(client, "A", "42.00", "open")
        r = _open(client, "A", "43.00", "open")
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "idempotency_conflict"
