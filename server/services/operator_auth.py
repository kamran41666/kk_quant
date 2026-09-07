"""Configurable operator-token dependency for high-risk local APIs."""
from __future__ import annotations

import hmac

from fastapi import HTTPException, Request

from server.config import settings


def operator_auth_configured() -> bool:
    return bool(settings.operator_token)


def require_operator(request: Request) -> str:
    """Require a constant-time compared token when the deployment config sets one.

    The default empty token intentionally keeps the loopback-only development
    experience usable.  Production or broker-enabled deployments must set a
    high-entropy ``QUANT_OPERATOR_TOKEN``; an empty value never authenticates
    a request when a token is configured.
    """
    expected = settings.operator_token
    if not expected:
        client = request.client
        if client is None or client.host not in {"127.0.0.1", "::1", "localhost"}:
            raise HTTPException(status_code=401, detail="operator_auth_required",
                                headers={"WWW-Authenticate": "X-Operator-Token"})
        return "local-development"
    supplied = request.headers.get("X-Operator-Token", "")
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="operator_auth_required",
                            headers={"WWW-Authenticate": "X-Operator-Token"})
    return "operator-token"
