import logging

import pytest
from fastapi.testclient import TestClient

from ledger.api import create_app
from ledger.service import LedgerService
from ledger.store import Store

logger = logging.getLogger("ledger.tests")


@pytest.fixture()
def service() -> LedgerService:
    return LedgerService(Store(":memory:"))


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_app(":memory:"))


@pytest.fixture(autouse=True)
def log_case(request):
    logger.info("==== 开始用例: %s ====", request.node.nodeid)
    yield
    logger.info("==== 结束用例: %s ====", request.node.nodeid)
