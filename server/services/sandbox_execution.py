"""Durable local Phase 3B sandbox session registry.

This service deliberately has no broker credentials or network side effects.
A session is a safe execution harness for exercising the broker-neutral
``LiveBrokerGateway`` contract from the UI. State is persisted as an atomic,
JSON-safe checkpoint under the configured data directory, while the explicit
restart operation rehydrates a checkpoint so recovery and event-cursor
behavior can be verified without contacting a broker.
"""
from __future__ import annotations

import threading
import uuid
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

from quant_engine.trading.gateway import OrderIntent
from quant_engine.trading.sandbox import SandboxBrokerGateway
from server.config import settings

_lock = threading.RLock()
_sessions: dict[str, SandboxBrokerGateway] = {}
_MAX_SESSIONS = 20
_SESSION_DIR = Path(settings.data_dir) / "sandbox_sessions"


class SandboxPersistenceError(RuntimeError):
    """Raised when a sandbox checkpoint cannot be durably written."""


class SandboxSessionCorruptedError(SandboxPersistenceError):
    """Raised when a persisted sandbox checkpoint cannot be decoded safely."""


def _prepare_session_store() -> None:
    try:
        _SESSION_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SandboxPersistenceError("sandbox session persistence failed") from exc


def _session_paths() -> tuple[Path, ...]:
    try:
        if not _SESSION_DIR.exists():
            return ()
        return tuple(_SESSION_DIR.glob("sbx-*.json"))
    except OSError as exc:
        raise SandboxPersistenceError("sandbox session persistence failed") from exc


def _session_id() -> str:
    return f"sbx-{uuid.uuid4().hex[:12]}"


def _report_dict(report) -> dict[str, Any]:
    return {
        "intent_id": report.intent_id,
        "status": report.status.value,
        "code": report.code,
        "side": report.side,
        "requested_quantity": report.requested_quantity,
        "filled_quantity": report.filled_quantity,
        "fill_price": report.fill_price,
        "reason": report.reason,
        "received_at": report.received_at,
        "broker_order_id": report.broker_order_id,
        "event_cursor": report.event_cursor,
    }


def _get(session_id: str) -> SandboxBrokerGateway:
    gateway = _sessions.get(session_id)
    if gateway is None:
        path = _session_path(session_id)
        if path.exists():
            try:
                gateway = SandboxBrokerGateway.from_checkpoint(json.loads(path.read_text(encoding="utf-8")))
                _sessions[session_id] = gateway
            except (OSError, ValueError, TypeError, KeyError, AttributeError,
                    IndexError, OverflowError) as exc:
                raise SandboxSessionCorruptedError("sandbox session is corrupted") from exc
    if gateway is None:
        raise KeyError("sandbox session not found")
    return gateway


def _session_path(session_id: str) -> Path:
    if not session_id.startswith("sbx-") or not all(char.isalnum() or char == "-" for char in session_id):
        raise KeyError("sandbox session not found")
    return _SESSION_DIR / f"{session_id}.json"


def _persist(session_id: str, gateway: SandboxBrokerGateway) -> None:
    temporary: Optional[str] = None
    fd: Optional[int] = None
    try:
        _prepare_session_store()
        target = _session_path(session_id)
        fd, temporary = tempfile.mkstemp(prefix=f".{session_id}.", suffix=".tmp", dir=str(_SESSION_DIR))
        handle = os.fdopen(fd, "w", encoding="utf-8")
        fd = None  # the file object now owns the descriptor
        with handle:
            json.dump(gateway.checkpoint(), handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except SandboxPersistenceError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise SandboxPersistenceError("sandbox session persistence failed") from exc
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if temporary and os.path.exists(temporary):
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _summary(session_id: str, gateway: SandboxBrokerGateway) -> dict[str, Any]:
    snapshot = gateway.snapshot()
    checkpoint = gateway.checkpoint()
    return {
        "id": session_id,
        "provider": gateway.capabilities.provider,
        "display_name": gateway.capabilities.display_name,
        "capabilities": gateway.capabilities.__dict__,
        "snapshot": {
            "cash": snapshot.cash,
            "market_value": snapshot.market_value,
            "equity": snapshot.equity,
            "positions": dict(snapshot.positions),
        },
        "open_orders": len(gateway.open_orders()),
        "event_cursor": checkpoint.get("cursor", 0),
        "faults": dict(checkpoint.get("faults", {})),
    }


def create_session(*, initial_cash: float, initial_fill_ratio: float = 1.0) -> dict[str, Any]:
    with _lock:
        _prepare_session_store()
        persisted_count = len(_session_paths())
        if max(len(_sessions), persisted_count) >= _MAX_SESSIONS:
            raise ValueError("sandbox_session_limit_reached")
        session_id = _session_id()
        gateway = SandboxBrokerGateway(initial_cash, initial_fill_ratio=initial_fill_ratio)
        # Persist before publishing the session so a failed write cannot leave
        # an API-visible state that disappears after a process restart.
        _persist(session_id, gateway)
        _sessions[session_id] = gateway
        return _summary(session_id, gateway)


def list_sessions() -> list[dict[str, Any]]:
    with _lock:
        for path in _session_paths():
            session_id = path.stem
            if session_id not in _sessions:
                try:
                    _get(session_id)
                except KeyError:
                    continue
        return [_summary(session_id, gateway) for session_id, gateway in _sessions.items()]


def get_session(session_id: str) -> dict[str, Any]:
    with _lock:
        return _summary(session_id, _get(session_id))


def set_price(session_id: str, *, code: str, price: float) -> dict[str, Any]:
    with _lock:
        gateway = _get(session_id)
        previous = gateway.checkpoint()
        try:
            gateway.set_price(code, price)
            _persist(session_id, gateway)
        except Exception:
            _sessions[session_id] = SandboxBrokerGateway.from_checkpoint(previous)
            raise
        return _summary(session_id, gateway)


def submit_order(session_id: str, *, intent_id: str, code: str, side: str,
                 quantity: int, limit_price: Optional[float] = None) -> dict[str, Any]:
    with _lock:
        gateway = _get(session_id)
        previous = gateway.checkpoint()
        try:
            report = gateway.submit(OrderIntent(intent_id, code, side, quantity, limit_price))
            _persist(session_id, gateway)
        except Exception:
            _sessions[session_id] = SandboxBrokerGateway.from_checkpoint(previous)
            raise
        return _report_dict(report)


def set_fault(session_id: str, *, operation: str, active: bool) -> dict[str, Any]:
    """Toggle one deterministic outage in the local sandbox.

    The mutation is checkpointed just like prices and orders.  If the write
    fails, the previous gateway is restored so the process-local registry and
    the durable checkpoint cannot diverge.
    """
    with _lock:
        gateway = _get(session_id)
        previous = gateway.checkpoint()
        try:
            gateway.set_fault(operation, active)
            _persist(session_id, gateway)
        except Exception:
            _sessions[session_id] = SandboxBrokerGateway.from_checkpoint(previous)
            raise
        return _summary(session_id, gateway)


def advance(session_id: str) -> list[dict[str, Any]]:
    with _lock:
        gateway = _get(session_id)
        previous = gateway.checkpoint()
        try:
            reports = gateway.advance()
            _persist(session_id, gateway)
        except Exception:
            _sessions[session_id] = SandboxBrokerGateway.from_checkpoint(previous)
            raise
        return [_report_dict(report) for report in reports]


def cancel_order(session_id: str, broker_order_id: str) -> dict[str, Any]:
    with _lock:
        gateway = _get(session_id)
        previous = gateway.checkpoint()
        try:
            report = gateway.cancel(broker_order_id)
            _persist(session_id, gateway)
        except Exception:
            _sessions[session_id] = SandboxBrokerGateway.from_checkpoint(previous)
            raise
        return _report_dict(report)


def list_orders(session_id: str) -> list[dict[str, Any]]:
    with _lock:
        gateway = _get(session_id)
        checkpoint = gateway.checkpoint()
        return [dict(item, session_id=session_id) for item in checkpoint.get("orders", [])]


def list_events(session_id: str, since: Optional[str] = None) -> list[dict[str, Any]]:
    with _lock:
        return [_report_dict(report) for report in _get(session_id).reconcile(since)]


def restart_session(session_id: str) -> dict[str, Any]:
    """Rebuild one session from its checkpoint and return its new summary."""
    with _lock:
        gateway = _get(session_id)
        restored = SandboxBrokerGateway.from_checkpoint(gateway.checkpoint())
        try:
            _persist(session_id, restored)
        except Exception:
            # The old in-memory gateway remains authoritative if the new
            # checkpoint cannot be written.
            raise
        _sessions[session_id] = restored
        return _summary(session_id, restored)


def clear_sessions(*, purge: bool = False) -> None:
    """Test-only cleanup hook; no API route exposes it."""
    with _lock:
        _sessions.clear()
        if purge and _SESSION_DIR.exists():
            for path in _SESSION_DIR.glob("sbx-*.json"):
                path.unlink(missing_ok=True)
