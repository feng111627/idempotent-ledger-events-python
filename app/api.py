"""HTTP 路由与异常处理。"""
from __future__ import annotations

from fastapi import APIRouter, FastAPI, Request

from app.service import LedgerService

router = APIRouter()


def register_exception_handlers(app: FastAPI) -> None:
    """把归一化业务错误翻译成统一结构的 JSON 响应（骨架阶段暂为 no-op）。"""
    return None


def get_service(request: Request) -> LedgerService:
    raise NotImplementedError


@router.post("/accounts", status_code=201)
def create_account(payload: dict) -> dict:
    raise NotImplementedError


@router.get("/accounts/{account_id}")
def get_account(account_id: str) -> dict:
    raise NotImplementedError


@router.post("/transfers", status_code=201)
def transfer(payload: dict) -> dict:
    raise NotImplementedError


@router.get("/events")
def list_events(account_id: str | None = None) -> dict:
    raise NotImplementedError


@router.post("/replay")
def replay(account_id: str | None = None) -> dict:
    raise NotImplementedError
