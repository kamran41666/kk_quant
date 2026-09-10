"""M2 persistence and replay tests for the manual A-share execution chain."""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from server.models.database import Base
from server.models.schema import ManualCashEvent, ManualExecutionEvent, ManualLedgerEvent, ManualPositionLot
from server.services.manual_ledger import (
    ManualLedgerError,
    create_manual_account,
    get_manual_state,
    record_cash_event,
    record_execution_event,
    record_fill_correction,
    reconcile_account,
    verify_ledger_hash_chain,
)


class WeekdayCalendar:
    def next_trading_day(self, value: date) -> date:
        result = value + timedelta(days=1)
        while result.weekday() >= 5:
            result += timedelta(days=1)
        return result


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    yield session
    session.close()
    Base.metadata.drop_all(engine)


def _account(db):
    account = create_manual_account(db, "实盘人工账户", broker_label="local-broker")
    record_cash_event(db, account.id, idempotency_key="open-1", event_type="opening_balance", amount="100000")
    return account


def test_buy_enforces_calendar_t_plus_one_and_rebuilds_cash_lot(db):
    account = _account(db)
    with pytest.raises(ManualLedgerError, match="calendar"):
        record_execution_event(
            db, account.id, client_event_id="buy-no-calendar", event_type="fill",
            code="600000.SH", side="buy", quantity=100, price="10",
            commission="5", traded_at="2024-01-02T08:00:00+08:00",
        )
    fill = record_execution_event(
        db, account.id, client_event_id="buy-1", user_trade_ref="broker-1", event_type="fill",
        code="600000.SH", side="buy", quantity=100, price="10",
        commission="5", traded_at="2024-01-02T08:00:00+08:00", calendar=WeekdayCalendar(),
    )
    state = get_manual_state(db, account.id, as_of="2024-01-02")
    assert fill.source == "user_reported"
    assert state["cash"] == Decimal("98995.00000000")
    assert state["positions"]["600000.SH"]["quantity"] == Decimal("100.00000000")
    assert state["positions"]["600000.SH"]["available_quantity"] == Decimal("0")
    assert verify_ledger_hash_chain(db, account.id)


def test_sell_respects_t_plus_one_then_accepts_odd_full_lot_and_fees(db):
    account = _account(db)
    record_execution_event(
        db, account.id, client_event_id="buy-2", event_type="fill", code="000001.SZ",
        side="buy", quantity=300, price="10", commission="5",
        traded_at="2024-01-02", calendar=WeekdayCalendar(),
    )
    with pytest.raises(ManualLedgerError, match="t_plus_one"):
        record_execution_event(
            db, account.id, client_event_id="sell-locked", event_type="partial_fill",
            code="000001.SZ", side="sell", quantity=100, price="11", commission="5",
            stamp_duty="0.55", other_fee="0.10", total_fee="5.65", traded_at="2024-01-02",
        )
    record_execution_event(
        db, account.id, client_event_id="sell-1", event_type="partial_fill", code="000001.SZ",
        side="sell", quantity=100, price="11", commission="5", stamp_duty="0.55",
        other_fee="0.10", total_fee="5.65", traded_at="2024-01-03",
    )
    record_execution_event(
        db, account.id, client_event_id="sell-2", event_type="fill", code="000001.SZ",
        side="sell", quantity=200, price="12", commission="5", stamp_duty="1.20",
        other_fee="0.10", total_fee="6.30", traded_at="2024-01-03",
    )
    state = get_manual_state(db, account.id)
    assert state["positions"] == {}
    # 100000 - 3005 + (1100 - 5.65) + (2400 - 6.30)
    assert state["cash"] == Decimal("100483.05000000")
    assert all(lot.remaining_quantity == Decimal("0") for lot in db.scalars(select(ManualPositionLot)).all())


def test_idempotency_trade_ref_and_correction_are_append_only(db):
    account = _account(db)
    fill = record_execution_event(
        db, account.id, client_event_id="same-client", user_trade_ref="same-ref", event_type="fill",
        code="600519.SH", side="buy", quantity=100, price="100", commission="5",
        traded_at="2024-01-02", calendar=WeekdayCalendar(),
    )
    assert record_execution_event(
        db, account.id, client_event_id="same-client", user_trade_ref="same-ref", event_type="fill",
        code="600519.SH", side="buy", quantity=100, price="100", commission="5",
        traded_at="2024-01-02", calendar=WeekdayCalendar(),
    ).id == fill.id
    with pytest.raises(ManualLedgerError, match="idempotency"):
        record_execution_event(
            db, account.id, client_event_id="same-client", event_type="fill", code="600519.SH",
            side="buy", quantity=200, price="100", commission="5", traded_at="2024-01-02",
            calendar=WeekdayCalendar(),
        )
    with pytest.raises(ManualLedgerError, match="idempotency"):
        record_execution_event(
            db, account.id, client_event_id="another-client", user_trade_ref="same-ref", event_type="fill",
            code="600519.SH", side="buy", quantity=100, price="100", commission="5",
            traded_at="2024-01-02", calendar=WeekdayCalendar(),
        )
    corrected = record_fill_correction(
        db, account.id, original_event_id=fill.id, replacement_client_event_id="corrected-1",
        side="buy", code="600519.SH", quantity=100, price="101", commission="5",
        traded_at="2024-01-02", calendar=WeekdayCalendar(),
    )
    assert corrected.event_type == "fill_correction"
    assert db.query(ManualExecutionEvent).count() == 3
    assert db.query(ManualLedgerEvent).count() == 4  # opening + original + reversal + replacement
    assert get_manual_state(db, account.id)["cash"] == Decimal("89895.00000000")
    assert verify_ledger_hash_chain(db, account.id)


def test_cash_events_and_reconciliation_are_user_reported(db):
    account = create_manual_account(db, "账户")
    opening = record_cash_event(db, account.id, idempotency_key="opening", event_type="opening_balance", amount="1000", occurred_at="2024-01-02")
    assert opening.source == "user_reported"
    record_cash_event(db, account.id, idempotency_key="deposit", event_type="deposit", amount="500", occurred_at="2024-01-03")
    record_cash_event(db, account.id, idempotency_key="withdraw", event_type="withdrawal", amount="-100", occurred_at="2024-01-04")
    assert db.query(ManualCashEvent).count() == 3
    assert get_manual_state(db, account.id)["cash"] == Decimal("1400.00000000")
    recon = reconcile_account(
        db, account.id, idempotency_key="statement-1", as_of="2024-01-04",
        cash="1400", total_asset="1400",
    )
    assert recon.status == "matched"
    assert db.get(type(recon), recon.id).ledger_checkpoint_hash


def test_same_code_sell_is_scoped_to_cohort_and_fractional_shares_are_rejected(db):
    account = _account(db)
    for cohort_id in ("cohort-a", "cohort-b"):
        record_execution_event(
            db, account.id, client_event_id=f"buy-{cohort_id}", event_type="fill",
            code="600000.SH", cohort_id=cohort_id, side="buy", quantity=100, price="10",
            traded_at="2024-01-02", calendar=WeekdayCalendar(),
        )
    record_execution_event(
        db, account.id, client_event_id="sell-cohort-a", event_type="fill",
        code="600000.SH", cohort_id="cohort-a", side="sell", quantity=100, price="11",
        traded_at="2024-01-03",
    )
    remaining = db.scalars(select(ManualPositionLot).where(ManualPositionLot.remaining_quantity > 0)).all()
    assert [(lot.cohort_id, lot.remaining_quantity) for lot in remaining] == [("cohort-b", Decimal("100.00000000"))]
    with pytest.raises(ManualLedgerError, match="integer_shares"):
        record_execution_event(
            db, account.id, client_event_id="fractional", event_type="fill",
            code="600000.SH", cohort_id="cohort-b", side="sell", quantity="0.5", price="11",
            traded_at="2024-01-03",
        )


def test_utc_timestamp_uses_shanghai_trade_date_for_t_plus_one(db):
    account = _account(db)
    record_execution_event(
        db, account.id, client_event_id="utc-crossing-buy", event_type="fill",
        code="000001.SZ", cohort_id="utc-cohort", side="buy", quantity=100, price="10",
        traded_at="2024-01-01T16:30:00+00:00", calendar=WeekdayCalendar(),
    )
    lot = db.scalars(select(ManualPositionLot).where(ManualPositionLot.cohort_id == "utc-cohort")).one()
    assert lot.buy_at == "2024-01-02"
    assert lot.unlock_date == "2024-01-03"
