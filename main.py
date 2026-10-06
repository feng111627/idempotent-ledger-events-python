"""应用入口：组装 FastAPI、数据库连接与路由。"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from app import db
from app.api import register_exception_handlers, router
from app.repository import LedgerRepository
from app.service import LedgerService

DB_PATH = Path(os.environ.get("LEDGER_DB_PATH", "ledger.db"))


def create_app(db_path: str | Path | None = None) -> FastAPI:
    path = Path(db_path) if db_path is not None else DB_PATH

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        conn = db.connect(path)
        db.init_db(conn)
        app.state.repo = LedgerRepository(conn)
        app.state.service = LedgerService(app.state.repo)
        try:
            yield
        finally:
            conn.close()

    app = FastAPI(title="本地幂等资金事件账本", lifespan=lifespan)
    register_exception_handlers(app)
    app.include_router(router)
    return app


app = create_app()


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
