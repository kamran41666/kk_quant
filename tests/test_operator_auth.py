import pytest
from fastapi import HTTPException
from starlette.requests import Request

from server.config import settings
from server.services.operator_auth import require_operator


def _request(token: str | None = None) -> Request:
    headers = [] if token is None else [(b"x-operator-token", token.encode())]
    return Request({"type": "http", "headers": headers, "client": ("127.0.0.1", 12345)})


def test_operator_auth_is_noop_only_when_deployment_token_is_empty(monkeypatch):
    monkeypatch.setattr(settings, "operator_token", "")
    assert require_operator(_request()) == "local-development"


def test_operator_auth_rejects_missing_or_wrong_token(monkeypatch):
    monkeypatch.setattr(settings, "operator_token", "correct-token")
    for token in (None, "wrong-token"):
        with pytest.raises(HTTPException) as exc:
            require_operator(_request(token))
        assert exc.value.status_code == 401
        assert exc.value.detail == "operator_auth_required"


def test_operator_auth_accepts_exact_token(monkeypatch):
    monkeypatch.setattr(settings, "operator_token", "correct-token")
    assert require_operator(_request("correct-token")) == "operator-token"


def test_empty_token_rejects_non_loopback_source(monkeypatch):
    monkeypatch.setattr(settings, "operator_token", "")
    request = Request({"type": "http", "headers": [], "client": ("10.0.0.8", 12345)})
    with pytest.raises(HTTPException) as exc:
        require_operator(request)
    assert exc.value.status_code == 401
