from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from server.models.database import Base
from server.models.schema import FundNavEvidence, PaperFill, PaperLedgerEvent, PaperLot, PaperOrder
from server.services.paper_trading import (
    account_snapshot,
    create_account,
    list_ledger,
    mark_to_market,
    submit_order,
    _validate_quote,
)
from server.services.fund_nav_registry import clear_fund_nav_registry, register_fund_nav


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_fund_account_accepts_fractional_units_and_persists_cny_ledger():
    db = _db()
    account = create_account(
        db, name="基金观察账户", market="cn-fund", initial_capital=10_000,
        max_position_weight=1.0,
    )
    assert account["market"] == "cn-fund"
    assert account["currency"] == "CNY"
    assert account["asset_type"] == "fund"

    register_fund_nav(code="110022", price=2.0, source="eastmoney:fund_nav",
                      as_of="2024-01-02T00:00:00+00:00", freshness="stale", db=db)
    bought = submit_order(
        db, account_id=account["id"], market="cn-fund", idempotency_key="fund-buy-0001",
        code="fund:110022", side="buy", quantity=1.25, price=2.0,
        price_source="eastmoney:fund_nav", price_as_of="2024-01-02T00:00:00+00:00",
        price_freshness="stale",
    )
    assert bought["status"] == "filled"
    assert bought["market"] == "cn-fund"
    assert bought["currency"] == "CNY"
    order = db.query(PaperOrder).one()
    fill = db.query(PaperFill).one()
    lot = db.query(PaperLot).one()
    assert order.code == fill.code == lot.code == "110022"
    assert order.quantity == pytest.approx(1.25)
    assert order.market == fill.market == lot.market == "cn-fund"
    assert lot.unlock_date == date.today().isoformat()

    # Fund NAV can be an official stale end-of-day observation, but its source
    # timestamp remains part of the durable audit trail.
    register_fund_nav(code="110022", price=2.1, source="eastmoney:fund_nav",
                      as_of="2024-01-02T00:00:00+00:00", freshness="stale", db=db)
    sold = submit_order(
        db, account_id=account["id"], idempotency_key="fund-sell-0001",
        code="110022", side="sell", quantity=0.25, price=2.1,
        price_source="eastmoney:fund_nav", price_as_of="2024-01-02T00:00:00+00:00",
        price_freshness="stale",
    )
    assert sold["status"] == "filled"
    snapshot = account_snapshot(db, account["id"])
    assert snapshot["positions"][0]["shares"] == pytest.approx(1.0)
    assert snapshot["market"] == "cn-fund"
    assert list_ledger(db, account["id"])[0]["currency"] == "CNY"


def test_us_account_accepts_whole_shares_and_uses_usd_without_t1_lock():
    db = _db()
    account = create_account(
        db, name="US paper account", market="us-equity", initial_capital=10_000,
        max_position_weight=1.0,
    )
    bought = submit_order(
        db, account_id=account["id"], market="us-equity", idempotency_key="us-buy-0001",
        code="aapl", side="buy", quantity=2, price=100.0,
    )
    assert bought["status"] == "filled"
    assert bought["code"] == "AAPL"
    assert bought["currency"] == "USD"
    sold = submit_order(
        db, account_id=account["id"], idempotency_key="us-sell-0001",
        code="AAPL", side="sell", quantity=1, price=101.0,
    )
    assert sold["status"] == "filled"
    assert account_snapshot(db, account["id"])["positions"][0]["shares"] == 1


def test_market_rules_fail_closed_for_mismatched_symbols_quantities_and_markets():
    db = _db()
    fund = create_account(db, name="fund", market="cn-fund", initial_capital=10_000,
                          max_position_weight=1.0)
    with pytest.raises(ValueError, match="quantity_below_minimum"):
        submit_order(db, account_id=fund["id"], idempotency_key="fund-too-small",
                     code="110022", side="buy", quantity=0.001, price=2.0)
    with pytest.raises(ValueError, match="domestic fund paper orders.*0.01"):
        submit_order(db, account_id=fund["id"], idempotency_key="fund-bad-step",
                     code="110022", side="buy", quantity=0.015, price=2.0)
    with pytest.raises(ValueError, match="six digits"):
        submit_order(db, account_id=fund["id"], idempotency_key="fund-bad-code",
                     code="AAPL", side="buy", quantity=1.0, price=2.0)
    with pytest.raises(ValueError, match="order_market_must_match_account"):
        submit_order(db, account_id=fund["id"], market="us-equity",
                     idempotency_key="fund-wrong-market", code="110022",
                     side="buy", quantity=1.0, price=2.0)

    us = create_account(db, name="us", market="us-equity", initial_capital=10_000,
                        max_position_weight=1.0)
    with pytest.raises(ValueError, match="whole shares"):
        submit_order(db, account_id=us["id"], idempotency_key="us-fractional",
                     code="MSFT", side="buy", quantity=1.5, price=100.0)
    with pytest.raises(ValueError, match="US equity symbol"):
        submit_order(db, account_id=us["id"], idempotency_key="us-a-code",
                     code="000001.SZ", side="buy", quantity=1, price=10.0)

    a_share = create_account(db, name="index guard", market="a-share", initial_capital=10_000,
                             max_position_weight=1.0)
    with pytest.raises(ValueError, match="index_not_tradable"):
        submit_order(db, account_id=a_share["id"], idempotency_key="index-paper-order",
                     code="000001.SH", side="buy", quantity=100, price=3900.0)


def test_fund_manual_price_and_non_trading_session_are_rejected():
    db = _db()
    fund = create_account(db, name="fund guard", market="cn-fund", initial_capital=10_000,
                          max_position_weight=1.0)
    with pytest.raises(ValueError, match="fund_order_requires_official_nav"):
        submit_order(db, account_id=fund["id"], idempotency_key="fund-manual-price",
                     code="110022", side="buy", quantity=1.0, price=2.0)

    a_share = create_account(db, name="calendar guard", initial_capital=10_000,
                             max_position_weight=1.0)
    with pytest.raises(ValueError, match="market_not_trading_day"):
        submit_order(db, account_id=a_share["id"], idempotency_key="a-share-weekend",
                     code="000001.SZ", side="buy", quantity=100, price=10.0,
                     trade_date=date(2024, 1, 6))


def test_fund_nav_must_match_registered_evidence_and_trade_date():
    db = _db()
    fund = create_account(db, name="fund evidence guard", market="cn-fund", initial_capital=10_000,
                          max_position_weight=1.0)
    register_fund_nav(code="110022", price=999.0, source="eastmoney:fund_nav",
                      as_of="2024-01-02T00:00:00+00:00", freshness="stale", db=db)
    with pytest.raises(ValueError, match="fund_nav_not_verified"):
        submit_order(db, account_id=fund["id"], idempotency_key="fund-spoofed-price",
                     code="110022", side="buy", quantity=1.0, price=999.1,
                     price_source="eastmoney:fund_nav", price_as_of="2024-01-02T00:00:00+00:00",
                     price_freshness="stale", trade_date=date(2024, 6, 14))


def test_fund_nav_rejects_future_same_day_timestamp():
    db = _db()
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    register_fund_nav(code="110022", price=2.0, source="eastmoney:fund_nav",
                      as_of=future, freshness="stale", db=db)
    with pytest.raises(ValueError, match="quote_timestamp_in_future"):
        _validate_quote(price=2.0, price_source="eastmoney:fund_nav",
                        price_as_of=future, price_freshness="stale",
                        market="cn-fund", code="110022", trade_date=date.today(), db=db)


def test_fund_nav_evidence_can_be_reused_after_memory_registry_reset():
    db = _db()
    fund = create_account(db, name="durable fund evidence", market="cn-fund", initial_capital=1_000,
                          max_position_weight=1.0)
    register_fund_nav(code="110022", price=2.0, source="eastmoney:fund_nav",
                      as_of="2024-01-02T00:00:00+00:00", freshness="stale", db=db)
    assert db.query(FundNavEvidence).count() == 1
    # Simulate a process restart: the in-memory fast path is gone, but the
    # paper service must still match the durable evidence row.
    clear_fund_nav_registry()
    order = submit_order(
        db, account_id=fund["id"], idempotency_key="durable-fund-order", code="110022", side="buy",
        quantity=1.0, price=2.0, price_source="eastmoney:fund_nav",
        price_as_of="2024-01-02T00:00:00+00:00", price_freshness="stale",
    )
    assert order["status"] == "filled"

    register_fund_nav(code="110022", price=2.0, source="eastmoney:fund_nav",
                      as_of="2026-09-05T00:00:00+00:00", freshness="stale", db=db)
    with pytest.raises(ValueError, match="fund_nav_date_in_future"):
        submit_order(db, account_id=fund["id"], idempotency_key="fund-future-nav",
                     code="110022", side="buy", quantity=1.0, price=2.0,
                     price_source="eastmoney:fund_nav", price_as_of="2026-09-05T00:00:00+00:00",
                     price_freshness="stale", trade_date=date(2024, 6, 14))


def test_cross_market_valuation_normalizes_symbols_and_preserves_fund_nav_staleness():
    db = _db()
    account = create_account(db, name="fund valuation", market="cn-fund",
                              initial_capital=1_000, max_position_weight=1.0)
    register_fund_nav(code="110022", price=2.0, source="eastmoney:fund_nav",
                      as_of="2024-01-02T00:00:00+00:00", freshness="stale", db=db)
    submit_order(db, account_id=account["id"], idempotency_key="fund-value-buy",
                 code="110022", side="buy", quantity=1.0, price=2.0,
                 price_source="eastmoney:fund_nav", price_as_of="2024-01-02T00:00:00+00:00",
                 price_freshness="stale")
    register_fund_nav(code="110022", price=2.2, source="eastmoney:fund_nav",
                      as_of="2024-01-02T00:00:00+00:00", freshness="stale", db=db)
    valuation = mark_to_market(
        db, account_id=account["id"], prices={"FUND:110022": 2.2},
        valuation_date=date.today(), price_source="eastmoney:fund_nav",
        price_as_of="2024-01-02T00:00:00+00:00", price_freshness="stale",
    )
    assert valuation["price_freshness"] == "stale"
    assert valuation["market_value"] == pytest.approx(2.2)
