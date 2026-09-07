import asyncio
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from fastapi import HTTPException

from server.api.paper import (
    CreatePaperAccountRequest,
    PaperOrderRequest,
    PaperValuationRequest,
    create_paper_account,
    create_paper_valuation,
    submit_paper_order,
)
from server.models.database import Base
from server.models.schema import FundNavEvidence
from server.api import market
from quant_engine.data.live import MarketQuote
from server.services.paper_trading import account_snapshot
from server.services.fund_nav_registry import clear_fund_nav_registry, register_fund_nav


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def test_api_creates_fund_account_and_fills_fractional_order(db):
    account = create_paper_account(
        CreatePaperAccountRequest(name="基金 API 账户", market="cn-fund", initial_capital=10_000,
                                   max_position_weight=1.0),
        db,
    )
    assert account["market"] == "cn-fund"
    assert account["currency"] == "CNY"

    register_fund_nav(code="110022", price=2.0, source="eastmoney:fund_nav",
                      as_of="2024-01-02T00:00:00Z", freshness="stale", db=db)
    payload = PaperOrderRequest(
        market="cn-fund", idempotency_key="api-fund-order-1", code="FUND:110022",
        side="buy", quantity=0.25, price=2.0,
        price_source="eastmoney:fund_nav", price_as_of="2024-01-02T00:00:00Z",
        price_freshness="stale", trade_date=date(2024, 6, 14),
    )
    order = asyncio.run(submit_paper_order(account["id"], payload, db))
    assert order["status"] == "filled"
    assert order["code"] == "110022"
    assert order["currency"] == "CNY"
    assert order["paper_only"] is True
    snapshot = account_snapshot(db, account["id"])
    assert snapshot["positions"][0]["shares"] == pytest.approx(0.25)


def test_api_uses_account_market_as_authority_and_returns_market_specific_errors(db):
    account = create_paper_account(
        CreatePaperAccountRequest(name="US API 账户", market="us-equity", initial_capital=10_000,
                                   max_position_weight=1.0),
        db,
    )

    wrong_market = PaperOrderRequest(
        market="cn-fund", idempotency_key="api-wrong-market", code="110022",
        side="buy", quantity=1, price=2,
    )
    with pytest.raises(HTTPException) as wrong_market_error:
        asyncio.run(submit_paper_order(account["id"], wrong_market, db))
    assert wrong_market_error.value.status_code == 409
    assert wrong_market_error.value.detail == "order_market_must_match_account"

    fractional = PaperOrderRequest(
        idempotency_key="api-fractional-us", code="AAPL", side="buy", quantity=0.5,
        price=100,
    )
    with pytest.raises(HTTPException) as fractional_error:
        asyncio.run(submit_paper_order(account["id"], fractional, db))
    assert fractional_error.value.status_code == 409
    assert "whole shares" in fractional_error.value.detail

    filled = asyncio.run(submit_paper_order(
        account["id"],
        PaperOrderRequest(idempotency_key="api-us-order-1", code="aapl", side="buy",
                          quantity=1, price=100, trade_date=date(2024, 6, 14)),
        db,
    ))
    assert filled["market"] == "us-equity"
    assert filled["currency"] == "USD"


def test_api_cross_market_valuation_accepts_fund_nav_symbol(db):
    account = create_paper_account(
        CreatePaperAccountRequest(name="估值账户", market="cn-fund", initial_capital=1_000,
                                   max_position_weight=1.0),
        db,
    )
    register_fund_nav(code="110022", price=2.0, source="eastmoney:fund_nav",
                      as_of="2024-01-02T00:00:00Z", freshness="stale", db=db)
    order = asyncio.run(submit_paper_order(
        account["id"],
        PaperOrderRequest(idempotency_key="api-fund-value-order", code="110022", side="buy",
                          quantity=1, price=2, price_source="eastmoney:fund_nav",
                          price_as_of="2024-01-02T00:00:00Z", price_freshness="stale",
                          trade_date=date(2024, 6, 14)),
        db,
    ))
    assert order["status"] == "filled"
    register_fund_nav(code="110022", price=2.2, source="eastmoney:fund_nav",
                      as_of="2024-01-02T00:00:00Z", freshness="stale", db=db)
    valuation = asyncio.run(create_paper_valuation(
        account["id"],
        PaperValuationRequest(valuation_date=date.today(), prices={"FUND:110022": 2.2},
                              price_source="eastmoney:fund_nav",
                              price_as_of="2024-01-02T00:00:00Z", price_freshness="stale"),
        db,
    ))
    assert valuation["market_value"] == pytest.approx(2.2)


def test_api_cross_market_valuation_accepts_per_position_nav_metadata(db):
    account = create_paper_account(
        CreatePaperAccountRequest(name="多基金证据账户", market="cn-fund", initial_capital=10_000,
                                   max_position_weight=1.0),
        db,
    )
    navs = {
        "110022": (2.0, "2024-01-02T00:00:00Z"),
        "161725": (0.5, "2024-01-03T00:00:00Z"),
    }
    for code, (price, as_of) in navs.items():
        register_fund_nav(code=code, price=price, source="eastmoney:fund_nav",
                          as_of=as_of, freshness="stale", db=db)
        order = asyncio.run(submit_paper_order(
            account["id"],
            PaperOrderRequest(idempotency_key=f"api-multi-fund-{code}", code=code,
                              side="buy", quantity=1, price=price,
                              price_source="eastmoney:fund_nav", price_as_of=as_of,
                              price_freshness="stale", trade_date=date(2024, 6, 14)),
            db,
        ))
        assert order["status"] == "filled"
    updates = {
        "110022": (2.1, "2024-01-04T00:00:00Z"),
        "161725": (0.6, "2024-01-05T00:00:00Z"),
    }
    for code, (price, as_of) in updates.items():
        register_fund_nav(code=code, price=price, source="eastmoney:fund_nav",
                          as_of=as_of, freshness="stale", db=db)
    valuation = asyncio.run(create_paper_valuation(
        account["id"],
        PaperValuationRequest(
            valuation_date=date.today(),
            prices={"FUND:110022": 2.1, "161725": 0.6},
            price_metadata={
                code: {"source": "eastmoney:fund_nav", "as_of": as_of, "freshness": "stale"}
                for code, (_price, as_of) in updates.items()
            },
            price_source="scheduler:mixed", price_freshness="mixed",
        ),
        db,
    ))
    assert valuation["market_value"] == pytest.approx(2.7)
    assert valuation["price_metadata"]["110022"]["as_of"] == "2024-01-04T00:00:00Z"
    assert valuation["paper_only"] is True


def test_api_rejects_weekend_orders_and_manual_fund_prices(db):
    a_share = create_paper_account(
        CreatePaperAccountRequest(name="A 股日历账户", market="a-share", initial_capital=10_000,
                                   max_position_weight=1.0), db,
    )
    with pytest.raises(HTTPException) as weekend:
        asyncio.run(submit_paper_order(
            a_share["id"],
            PaperOrderRequest(idempotency_key="api-weekend-order", code="000001.SZ",
                              side="buy", quantity=100, price=10, trade_date=date(2024, 1, 6)),
            db,
        ))
    assert weekend.value.status_code == 409
    assert weekend.value.detail == "market_not_trading_day"

    fund = create_paper_account(
        CreatePaperAccountRequest(name="基金净值边界账户", market="cn-fund", initial_capital=10_000,
                                   max_position_weight=1.0), db,
    )
    with pytest.raises(HTTPException) as manual:
        asyncio.run(submit_paper_order(
            fund["id"],
            PaperOrderRequest(idempotency_key="api-manual-fund", code="110022",
                              side="buy", quantity=1, price=2, trade_date=date(2024, 6, 14)),
            db,
        ))
    assert manual.value.status_code == 409
    assert manual.value.detail == "fund_order_requires_official_nav"


def test_market_fund_quote_ingestion_persists_evidence_for_paper_order(db, monkeypatch):
    now = datetime.now(timezone.utc).isoformat()

    class StubFundProvider:
        def fetch_quotes(self, symbols):
            return [MarketQuote(code="110022", name="基金", price=2.0, change_pct=0.1,
                                volume=None, amount=None, source="eastmoney:fund_nav",
                                as_of="2024-01-02T00:00:00Z", received_at=now,
                                freshness="stale", is_fallback=False, asset_type="fund",
                                market="CN", currency="CNY")]

    monkeypatch.setitem(market._cross_market_providers, "cn-fund", StubFundProvider())
    clear_fund_nav_registry()
    response = market.get_cross_market_quotes("cn-fund", "110022", db)
    assert response["meta"]["status"] == "ok"
    assert db.query(FundNavEvidence).count() == 1

    account = create_paper_account(
        CreatePaperAccountRequest(name="API 持久证据账户", market="cn-fund", initial_capital=1_000,
                                   max_position_weight=1.0), db,
    )
    clear_fund_nav_registry()
    order = asyncio.run(submit_paper_order(
        account["id"],
        PaperOrderRequest(idempotency_key="api-durable-fund-order", code="110022", side="buy", quantity=1,
                          price=2.0, price_source="eastmoney:fund_nav",
                          price_as_of="2024-01-02T00:00:00Z", price_freshness="stale",
                          trade_date=date(2024, 6, 14)),
        db,
    ))
    assert order["status"] == "filled"
