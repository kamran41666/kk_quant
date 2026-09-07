"""Safe local sandbox execution endpoints for Phase 3B verification."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from quant_engine.trading.sandbox import SandboxInjectedFailure

from server.services.sandbox_execution import (
    SandboxPersistenceError,
    advance,
    cancel_order,
    create_session,
    get_session,
    list_events,
    list_orders,
    list_sessions,
    restart_session,
    set_fault,
    set_price,
    submit_order,
)
from server.services.operator_auth import require_operator

router = APIRouter(prefix="/live/sandbox", tags=["live-sandbox"], dependencies=[Depends(require_operator)])


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateSandboxSessionRequest(_StrictModel):
    initial_cash: float = Field(gt=0, le=100_000_000)
    initial_fill_ratio: float = Field(default=1.0, ge=0, le=1)


class SandboxPriceRequest(_StrictModel):
    code: str = Field(pattern=r"^\d{6}\.(SH|SZ|BJ)$")
    price: float = Field(gt=0, le=10_000_000)


class SandboxOrderRequest(_StrictModel):
    # Keep the id safe for broker-neutral persistence and human-readable audit
    # output; free-form secrets must never be accepted as an order identifier.
    intent_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{7,159}$")
    code: str = Field(pattern=r"^\d{6}\.(SH|SZ|BJ)$")
    side: str = Field(pattern=r"^(buy|sell)$")
    quantity: int = Field(gt=0, le=10_000_000)
    limit_price: Optional[float] = Field(default=None, gt=0, le=10_000_000)

    @field_validator("quantity")
    @classmethod
    def a_share_lot(cls, value: int) -> int:
        if value % 100:
            raise ValueError("sandbox A-share orders must use 100-share lots")
        return value


class SandboxFaultRequest(_StrictModel):
    """Enable one deterministic outage for local recovery drills only."""

    operation: str = Field(pattern=r"^(submit|advance|reconcile)$")
    active: bool = True


def _not_found(exc: KeyError) -> HTTPException:
    return HTTPException(status_code=404, detail=str(exc))


def _conflict(exc: ValueError) -> HTTPException:
    return HTTPException(status_code=409, detail=str(exc))


def _unavailable() -> HTTPException:
    return HTTPException(status_code=503, detail="sandbox_persistence_unavailable")


def _fault_injected() -> HTTPException:
    # Do not expose exception text from an injected provider failure.  The
    # fixed detail is safe for clients and easy to assert in recovery tests.
    return HTTPException(status_code=503, detail="sandbox_fault_injected")


@router.get("/sessions")
def get_sandbox_sessions():
    try:
        return list_sessions()
    except SandboxPersistenceError as exc:
        raise _unavailable() from exc


@router.post("/sessions")
def create_sandbox_session(req: CreateSandboxSessionRequest):
    try:
        return create_session(initial_cash=req.initial_cash,
                              initial_fill_ratio=req.initial_fill_ratio)
    except SandboxPersistenceError as exc:
        raise _unavailable() from exc
    except ValueError as exc:
        raise _conflict(exc) from exc


@router.get("/sessions/{session_id}")
def get_sandbox_session(session_id: str):
    try:
        return get_session(session_id)
    except SandboxPersistenceError as exc:
        raise _unavailable() from exc
    except KeyError as exc:
        raise _not_found(exc) from exc


@router.post("/sessions/{session_id}/prices")
def set_sandbox_price(session_id: str, req: SandboxPriceRequest):
    try:
        return set_price(session_id, code=req.code, price=req.price)
    except SandboxPersistenceError as exc:
        raise _unavailable() from exc
    except KeyError as exc:
        raise _not_found(exc) from exc
    except ValueError as exc:
        raise _conflict(exc) from exc


@router.post("/sessions/{session_id}/orders")
def submit_sandbox_order(session_id: str, req: SandboxOrderRequest):
    try:
        return submit_order(session_id, intent_id=req.intent_id, code=req.code,
                            side=req.side, quantity=req.quantity,
                            limit_price=req.limit_price)
    except SandboxInjectedFailure as exc:
        raise _fault_injected() from exc
    except SandboxPersistenceError as exc:
        raise _unavailable() from exc
    except KeyError as exc:
        raise _not_found(exc) from exc
    except ValueError as exc:
        raise _conflict(exc) from exc


@router.post("/sessions/{session_id}/advance")
def advance_sandbox_orders(session_id: str):
    try:
        return advance(session_id)
    except SandboxInjectedFailure as exc:
        raise _fault_injected() from exc
    except SandboxPersistenceError as exc:
        raise _unavailable() from exc
    except KeyError as exc:
        raise _not_found(exc) from exc


@router.get("/sessions/{session_id}/orders")
def get_sandbox_orders(session_id: str):
    try:
        return list_orders(session_id)
    except SandboxPersistenceError as exc:
        raise _unavailable() from exc
    except KeyError as exc:
        raise _not_found(exc) from exc


@router.post("/sessions/{session_id}/orders/{broker_order_id}/cancel")
def cancel_sandbox_order(session_id: str, broker_order_id: str):
    try:
        return cancel_order(session_id, broker_order_id)
    except SandboxPersistenceError as exc:
        raise _unavailable() from exc
    except KeyError as exc:
        raise _not_found(exc) from exc


@router.get("/sessions/{session_id}/events")
def get_sandbox_events(session_id: str, since: Optional[str] = None):
    try:
        return list_events(session_id, since)
    except SandboxInjectedFailure as exc:
        raise _fault_injected() from exc
    except SandboxPersistenceError as exc:
        raise _unavailable() from exc
    except KeyError as exc:
        raise _not_found(exc) from exc
    except ValueError as exc:
        raise _conflict(exc) from exc


@router.post("/sessions/{session_id}/faults")
def set_sandbox_fault(session_id: str, req: SandboxFaultRequest):
    try:
        return set_fault(session_id, operation=req.operation, active=req.active)
    except SandboxPersistenceError as exc:
        raise _unavailable() from exc
    except KeyError as exc:
        raise _not_found(exc) from exc
    except ValueError as exc:
        raise _conflict(exc) from exc


@router.post("/sessions/{session_id}/restart")
def restart_sandbox_session(session_id: str):
    try:
        return restart_session(session_id)
    except SandboxPersistenceError as exc:
        raise _unavailable() from exc
    except KeyError as exc:
        raise _not_found(exc) from exc
