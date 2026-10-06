"""HTTP 路由与异常处理。"""
from __future__ import annotations

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.domain import Money
from app.errors import LedgerError
from app.schemas import CreateAccountRequest, TransferRequest
from app.service import LedgerService

router = APIRouter()


def _error_body(code: str, message: str, details=None) -> dict:
    body: dict = {"error": {"code": code, "message": message}}
    if details is not None:
        body["error"]["details"] = details
    return body


def register_exception_handlers(app: FastAPI) -> None:
    """业务错误与入参校验错误统一为 {error:{code,message}} 结构。"""

    @app.exception_handler(LedgerError)
    async def _handle_ledger(_: Request, exc: LedgerError):
        return JSONResponse(
            status_code=exc.http_status,
            content=_error_body(exc.code, str(exc)),
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(_: Request, exc: RequestValidationError):
        # 只回传字段级、可读的校验信息，不暴露内部堆栈/类型
        details = [
            {"field": ".".join(str(p) for p in e.get("loc", []) if p != "body"),
             "message": e.get("msg", "")}
            for e in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content=_error_body("validation_error", "请求参数校验失败", details),
        )


def get_service(request: Request) -> LedgerService:
    return request.app.state.service


@router.post("/accounts", status_code=201)
def create_account(payload: CreateAccountRequest, request: Request) -> dict:
    svc = get_service(request)
    opening = Money.from_str(payload.opening_balance).minor
    return svc.create_account(
        payload.account_id, opening, payload.idempotency_key)


@router.get("/accounts/{account_id}")
def get_account(account_id: str, request: Request) -> dict:
    return get_service(request).get_balance(account_id)


@router.post("/transfers", status_code=201)
def transfer(payload: TransferRequest, request: Request) -> dict:
    svc = get_service(request)
    return svc.transfer(
        payload.from_account_id,
        payload.to_account_id,
        Money.from_str(payload.amount).minor,
        payload.idempotency_key,
    )


@router.get("/events")
def list_events(request: Request, account_id: str | None = None) -> dict:
    events = get_service(request).list_events(account_id)
    return {"events": events, "count": len(events)}


@router.post("/replay")
def replay(request: Request, account_id: str | None = None) -> dict:
    return get_service(request).replay(account_id)
