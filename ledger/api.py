"""FastAPI 路由层：把 LedgerError 统一映射为 JSON 错误响应。

请求体校验失败（字段缺失/类型错误）也被归一化为与业务错误完全一致的
{"error": {"code", "message"}} 结构，不会冒出框架默认的其它格式。
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .errors import InvalidRequest, LedgerError, StorageError
from .models import format_cents, parse_amount_to_cents
from .service import LedgerService
from .store import Store

logger = logging.getLogger("ledger")


class CreateAccountRequest(BaseModel):
    account_id: str = Field(min_length=1, max_length=128)
    initial_balance: str = "0.00"
    # 可选：携带后开户按 account:{account_id} 作用域幂等
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=256)


class TransferRequest(BaseModel):
    from_account: str = Field(min_length=1, max_length=128)
    to_account: str = Field(min_length=1, max_length=128)
    amount: str
    idempotency_key: str = Field(min_length=1, max_length=256)


class ReversalRequest(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=256)


def create_app(db_path: str = ":memory:") -> FastAPI:
    app = FastAPI(title="Idempotent Ledger")
    service = LedgerService(Store(db_path))
    app.state.service = service

    @app.exception_handler(LedgerError)
    async def ledger_error_handler(_req: Request, exc: LedgerError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content=exc.to_dict())

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _req: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # 请求体不合法：归一化为与业务错误一致的结构
        details = "; ".join(
            f"{'.'.join(str(p) for p in e.get('loc', []))}: {e.get('msg', '')}"
            for e in exc.errors()
        )
        err = InvalidRequest(f"invalid request body: {details}")
        return JSONResponse(status_code=err.http_status, content=err.to_dict())

    @app.exception_handler(Exception)
    async def unknown_error_handler(_req: Request, exc: Exception) -> JSONResponse:
        # 归一化未知异常：不把框架/驱动原始报错暴露给调用方
        logger.exception("unexpected error: %s", type(exc).__name__)
        err = StorageError("internal error")
        return JSONResponse(status_code=500, content=err.to_dict())

    @app.post("/accounts", status_code=201)
    def create_account(req: CreateAccountRequest, response: Response) -> dict:
        # 0、0.0、0.00 等零金额写法都解析为同一个合法值 0
        initial = parse_amount_to_cents(req.initial_balance, allow_zero=True)
        account, replayed = service.create_account(
            req.account_id, initial, req.idempotency_key
        )
        if replayed:
            response.headers["X-Idempotent-Replay"] = "true"
        return {
            "account_id": account.account_id,
            "balance": format_cents(account.balance_cents),
            "created_at": account.created_at,
        }

    @app.get("/accounts/{account_id}")
    def get_account(account_id: str) -> dict:
        account = service.get_account(account_id)
        return {
            "account_id": account.account_id,
            "balance": format_cents(account.balance_cents),
            "created_at": account.created_at,
        }

    @app.post("/transfers")
    def transfer(req: TransferRequest, response: Response) -> dict:
        amount_cents = parse_amount_to_cents(req.amount)
        status, body, replayed = service.transfer(
            req.from_account, req.to_account, amount_cents, req.idempotency_key
        )
        response.status_code = status
        if replayed:
            response.headers["X-Idempotent-Replay"] = "true"
        return body

    @app.post("/transfers/{transfer_id}/reversal")
    def reverse_transfer(
        transfer_id: str, req: ReversalRequest, response: Response
    ) -> dict:
        status, body, replayed = service.reverse_transfer(
            transfer_id, req.idempotency_key
        )
        response.status_code = status
        if replayed:
            response.headers["X-Idempotent-Replay"] = "true"
        return body

    @app.get("/replay")
    def replay(account_id: str | None = None) -> dict:
        return service.replay(account_id)

    return app
