"""测试夹具：每个用例独立临时数据库，支持 HTTP 与服务层两种入口。"""
from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from app import db
from app.repository import LedgerRepository
from app.service import LedgerService
from main import create_app

# 让 pytest --log-cli 能清楚看到每个业务用例的执行轨迹
case_log = logging.getLogger("ledger.test")


@pytest.fixture
def database(tmp_path):
    dbase = db.Database(tmp_path / "test_ledger.db")
    dbase.init()
    try:
        yield dbase
    finally:
        dbase.close_current()


@pytest.fixture
def service(database) -> LedgerService:
    return LedgerService(LedgerRepository(database))


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / "http_ledger.db")
    with TestClient(app) as c:
        yield c


@pytest.fixture
def make_accounts(client):
    """工厂夹具：经 HTTP 批量开户，返回 (client, ids)。"""
    def _make(spec: dict[str, str]):
        for acct_id, opening in spec.items():
            r = client.post("/accounts", json={
                "account_id": acct_id,
                "opening_balance": opening,
                "idempotency_key": f"open-{acct_id}",
            })
            assert r.status_code == 201, r.text
        return client
    return _make
