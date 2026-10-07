"""FastAPI 路由层：把 LedgerError 统一映射为 JSON 错误响应。"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .errors import InvalidRequest, LedgerError, StorageError
from .models import (
    format_cents,
    parse_amount_to_cents,
    parse_initial_balance_to_cents,
)
from .service import LedgerService
from .store import Store

logger = logging.getLogger("ledger")


class CreateAccountRequest(BaseModel):
    account_id: str = Field(min_length=1, max_length=128)
    initial_balance: str | int | float = "0.00"
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=256)


class TransferRequest(BaseModel):
    from_account: str = Field(min_length=1, max_length=128)
    to_account: str = Field(min_length=1, max_length=128)
    amount: str | int | float
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
        # 请求体本身不合法（字段缺失/类型不对）也必须返回与业务错误
        # 完全一致的 {"error": {"code", "message"}} 结构
        err = InvalidRequest("request body is invalid")
        detail = "; ".join(
            ".".join(str(p) for p in e.get("loc", []) if p != "body")
            + ": " + str(e.get("msg", ""))
            for e in exc.errors()
        ).strip("; ")
        if detail:
            err.message = f"request body is invalid: {detail}"
        return JSONResponse(status_code=err.http_status, content=err.to_dict())

    @app.exception_handler(Exception)
    async def unknown_error_handler(_req: Request, exc: Exception) -> JSONResponse:
        # 归一化未知异常：不把框架/驱动原始报错暴露给调用方
        logger.exception("unexpected error: %s", type(exc).__name__)
        err = StorageError("internal error")
        return JSONResponse(status_code=500, content=err.to_dict())

    @app.post("/accounts", status_code=201)
    def create_account(req: CreateAccountRequest, response: Response) -> dict:
        # 0、0.0、0.00 等零金额写法统一归一化为 0 放行
        initial = parse_initial_balance_to_cents(req.initial_balance)
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
