"""测试夹具：每个用例使用独立临时数据库。"""
import pytest
from fastapi.testclient import TestClient

from main import create_app


@pytest.fixture
def client(tmp_path):
    db_file = tmp_path / "test_ledger.db"
    app = create_app(db_file)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def service(tmp_path):
    raise NotImplementedError
