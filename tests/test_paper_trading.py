from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from server.models.database import Base
from server.models.schema import PaperFill, PaperLot, PaperOrder
from server.services.paper_trading import account_report, account_snapshot, build_daily_report, create_account, list_ledger, list_orders, mark_to_market, record_deviation, reconcile_account, submit_order


def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_persistent_paper_order_is_idempotent_and_balanced():
    db = session()
    account = create_account(db, name="test", initial_capital=100_000, max_order_notional=20_000, max_position_weight=1.0)
    first = submit_order(db, account_id=account["id"], idempotency_key="order-0001", code="000001.SZ", side="buy", quantity=1_000, price=10)
    second = submit_order(db, account_id=account["id"], idempotency_key="order-0001", code="000001.SZ", side="buy", quantity=1_000, price=10)
    snapshot = account_snapshot(db, account["id"])

    assert first["status"] == "filled"
    assert second["id"] == first["id"]
    assert snapshot["cash"] == 89_995
    assert snapshot["equity"] == 99_995
    assert snapshot["positions"][0]["shares"] == 1_000
    assert len(list_orders(db, account["id"])) == 1
    assert list_ledger(db, account["id"])[0]["event_type"] == "fill"


def test_persistent_paper_order_rejects_risk_without_cash_change():
    db = session()
    account = create_account(db, name="risk", initial_capital=100_000, max_order_notional=5_000, max_position_weight=1.0)
    report = submit_order(db, account_id=account["id"], idempotency_key="order-risk-1", code="000001.SZ", side="buy", quantity=1_000, price=10)
    assert report["status"] == "rejected"
    assert report["reject_reason"] == "max_order_notional_exceeded"
    assert account_snapshot(db, account["id"])["cash"] == 100_000


def test_idempotency_price_conflict_and_quote_provenance_guards():
    db = session()
    account = create_account(db, name="guards", initial_capital=100_000, max_position_weight=1.0)
    submit_order(db, account_id=account["id"], idempotency_key="order-guard-1", code="000001.SZ", side="buy", quantity=100, price=10)
    with pytest.raises(ValueError, match="idempotency_key"):
        submit_order(db, account_id=account["id"], idempotency_key="order-guard-1", code="000001.SZ", side="buy", quantity=100, price=11)
    with pytest.raises(ValueError, match="quote_stale_or_unknown"):
        submit_order(db, account_id=account["id"], idempotency_key="order-guard-2", code="000001.SZ", side="buy", quantity=100, price=10, price_source="feed", price_as_of="2020-01-01T00:00:00+00:00", price_freshness="fresh")


def test_t_plus_one_and_zero_cash_sell_after_unlock():
    db = session()
    account = create_account(db, name="t1", initial_capital=1005, max_position_weight=1.0)
    buy = submit_order(db, account_id=account["id"], idempotency_key="order-t1-buy", code="000001.SZ", side="buy", quantity=100, price=10)
    assert buy["status"] == "filled"
    locked = submit_order(db, account_id=account["id"], idempotency_key="order-t1-sell", code="000001.SZ", side="sell", quantity=100, price=10)
    assert locked["status"] == "rejected"
    assert locked["reject_reason"] == "t_plus_one_lock"
    lot = db.query(PaperLot).first()
    lot.unlock_date = date.today().isoformat()
    db.commit()
    sold = submit_order(db, account_id=account["id"], idempotency_key="order-t1-sell-2", code="000001.SZ", side="sell", quantity=100, price=10)
    assert sold["status"] == "filled"
    assert account_snapshot(db, account["id"])["cash"] == pytest.approx(994.0)


def test_daily_loss_uses_last_mark_to_market():
    db = session()
    account = create_account(db, name="breaker", initial_capital=100_000, max_position_weight=1.0, max_daily_loss=0.03)
    submit_order(db, account_id=account["id"], idempotency_key="order-loss-buy", code="000001.SZ", side="buy", quantity=5_000, price=10)
    mark_to_market(db, account_id=account["id"], prices={"000001.SZ": 10}, valuation_date=date(2024, 1, 1))
    mark_to_market(db, account_id=account["id"], prices={"000001.SZ": 1}, valuation_date=date(2024, 1, 2))
    blocked = submit_order(db, account_id=account["id"], idempotency_key="order-loss-buy-2", code="000001.SZ", side="buy", quantity=100, price=1)
    assert blocked["status"] == "rejected"
    assert blocked["reject_reason"] == "daily_loss_limit_exceeded"


def test_first_valuation_uses_initial_capital_as_loss_baseline():
    db = session()
    account = create_account(db, name="first-valuation", initial_capital=100_000, max_position_weight=1.0, max_daily_loss=0.03)
    submit_order(db, account_id=account["id"], idempotency_key="order-first-loss", code="000001.SZ", side="buy", quantity=5_000, price=10)
    valuation = mark_to_market(db, account_id=account["id"], prices={"000001.SZ": 1}, valuation_date=date(2024, 1, 1))
    assert valuation["daily_return"] < -0.03
    blocked = submit_order(db, account_id=account["id"], idempotency_key="order-first-loss-2", code="000001.SZ", side="buy", quantity=100, price=1)
    assert blocked["reject_reason"] == "daily_loss_limit_exceeded"


def test_account_report_is_derived_from_durable_rows():
    db = session()
    account = create_account(db, name="report", initial_capital=100_000, max_position_weight=1.0)
    submit_order(db, account_id=account["id"], idempotency_key="order-report-1", code="000001.SZ", side="buy", quantity=100, price=10)
    mark_to_market(db, account_id=account["id"], prices={"000001.SZ": 10}, valuation_date=date(2024, 1, 1))
    report = account_report(db, account["id"])
    assert report["valuation_count"] == 1
    assert report["order_count"] == 1
    assert report["fill_count"] == 1


def test_account_report_without_valuation_includes_positions():
    db = session()
    account = create_account(db, name="report-current", initial_capital=100_000, max_position_weight=1.0)
    submit_order(db, account_id=account["id"], idempotency_key="order-report-current", code="000001.SZ", side="buy", quantity=100, price=10)
    report = account_report(db, account["id"])
    assert report["equity"] == pytest.approx(99_995.0)
    assert report["total_return"] == pytest.approx(-0.00005)


def test_deviation_daily_report_and_reconciliation_are_persistent():
    db = session()
    account = create_account(db, name="deviation", initial_capital=100_000, max_position_weight=1.0)
    submit_order(db, account_id=account["id"], idempotency_key="order-deviation", code="000001.SZ", side="buy", quantity=100, price=10)
    mark_to_market(db, account_id=account["id"], prices={"000001.SZ": 10}, valuation_date=date(2024, 1, 1))
    deviation = record_deviation(db, account_id=account["id"], valuation_date=date(2024, 1, 1), expected_return=0.01)
    report = build_daily_report(db, account_id=account["id"], report_date=date(2024, 1, 1))
    reconciliation = reconcile_account(db, account["id"])
    assert deviation["tracking_error"] < 0
    assert report["reconciliation_status"] == "ok"
    assert reconciliation["status"] == "ok"


def test_daily_report_is_calculated_as_of_report_date():
    db = session()
    account = create_account(db, name="historical-report", initial_capital=100_000, max_position_weight=1.0)
    # Establish a historical cash-only checkpoint, then create a trade today.
    # The later trade must not change the historical report's return/counts.
    mark_to_market(db, account_id=account["id"], prices={}, valuation_date=date(2024, 1, 1))
    submit_order(db, account_id=account["id"], idempotency_key="order-after-report", code="000001.SZ", side="buy", quantity=100, price=10)
    report = build_daily_report(db, account_id=account["id"], report_date=date(2024, 1, 1))
    assert report["total_return"] == pytest.approx(0.0)
    assert report["max_drawdown"] == pytest.approx(0.0)
    assert report["order_count"] == 0
    assert report["fill_count"] == 0


def test_daily_report_includes_fractional_second_before_next_midnight():
    db = session()
    account = create_account(db, name="report-cutoff", initial_capital=100_000, max_position_weight=1.0)
    mark_to_market(db, account_id=account["id"], prices={}, valuation_date=date(2024, 1, 1))
    submit_order(db, account_id=account["id"], idempotency_key="order-cutoff", code="000001.SZ", side="buy", quantity=100, price=10)
    order = db.query(PaperOrder).filter(PaperOrder.account_id == account["id"]).one()
    fill = db.query(PaperFill).filter(PaperFill.account_id == account["id"]).one()
    order.created_at = "2024-01-01T23:59:59.500000"
    fill.created_at = "2024-01-01T23:59:59.500000"
    db.commit()
    report = build_daily_report(db, account_id=account["id"], report_date=date(2024, 1, 1))
    assert report["order_count"] == 1
    assert report["fill_count"] == 1


def test_file_backed_account_transaction_uses_database_lock(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'paper.db'}")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    account = create_account(db, name="file-lock", initial_capital=10_000, max_position_weight=1.0)
    result = submit_order(db, account_id=account["id"], idempotency_key="order-file-lock", code="000001.SZ", side="buy", quantity=100, price=10)
    assert result["status"] == "filled"
