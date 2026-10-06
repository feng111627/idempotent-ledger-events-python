"""业务：底层驱动异常必须归一化，不透出原始报错给调用方。"""
from __future__ import annotations

import logging
import sqlite3

import pytest

from app.errors import StorageUnavailable
from app.repository import LedgerRepository
from app.service import LedgerService

log = logging.getLogger("ledger.test.errors")


class TestErrorNormalization:
    def test_closed_connection_normalized_to_storage_unavailable(self, tmp_path):
        # 准备一个正常建表的库，随后直接关闭底层连接制造驱动异常
        path = tmp_path / "broken.db"
        conn = sqlite3.connect(str(path))
        conn.executescript("""
        CREATE TABLE accounts(account_id TEXT PRIMARY KEY,
                              balance INTEGER NOT NULL, created_at TEXT NOT NULL);
        """)
        conn.commit()
        conn.close()

        # 以已关闭连接构造仓储，任何读/写都应被归一化
        closed = sqlite3.connect(str(path))
        closed.close()
        repo = LedgerRepository(closed)
        svc = LedgerService(repo)

        with pytest.raises(StorageUnavailable) as ei:
            svc.get_balance("A")
        msg = str(ei.value)
        log.info("[读异常归一化] 对外文案=%r code=%s",
                 msg, ei.value.code)
        assert ei.value.code == "storage_unavailable"
        # 不允许出现驱动名/SQL 等内部细节
        assert "sqlite" not in msg.lower()
        assert "SELECT" not in msg

        with pytest.raises(StorageUnavailable) as ei2:
            svc.create_account("A", 0, "k")
        assert ei2.value.code == "storage_unavailable"
        assert "sqlite" not in str(ei2.value).lower()
        log.info("[写异常归一化] 对外文案=%r（无驱动/SQL 细节）",
                 str(ei2.value))

    def test_http_error_envelope_is_uniform(self, client):
        """各类错误统一为 {error:{code,message}}，分类码有业务含义。"""
        # 404
        r = client.get("/accounts/none")
        assert set(r.json()) == {"error"}
        assert set(r.json()["error"]) == {"code", "message"}
        assert r.json()["error"]["code"] == "account_not_found"

        # 先建 A/B 触发透支 422
        client.post("/accounts", json={"account_id": "A",
                    "opening_balance": "1.00", "idempotency_key": "ka"})
        client.post("/accounts", json={"account_id": "B",
                    "opening_balance": "0", "idempotency_key": "kb"})
        r2 = client.post("/transfers", json={
            "from_account_id": "A", "to_account_id": "B",
            "amount": "9.00", "idempotency_key": "t"})
        body = r2.json()
        log.info("[HTTP 统一信封] 透支 -> status=%s code=%s",
                 r2.status_code, body["error"]["code"])
        assert r2.status_code == 422
        assert body["error"]["code"] == "insufficient_funds"
        assert "sqlite" not in body["error"]["message"].lower()
