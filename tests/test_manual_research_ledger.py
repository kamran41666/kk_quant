"""Cohort and capacity invariants for the H2b research execution ledger."""
from datetime import date
from decimal import Decimal

from quant_engine.backtest.manual_research_ledger import ManualResearchLedger, ResearchExecutionIntent


DAY1 = date(2027, 1, 4)
DAY2 = date(2027, 1, 5)
BAR = {"open": 10, "close": 10, "preclose": 10, "volume": 1_000_000, "is_suspended": False, "is_st": False}


def _intent(intent_id, cohort, side, quantity, day=DAY1, phase="open"):
    return ResearchExecutionIntent(
        intent_id=intent_id, trade_date=day, phase=phase, cohort_id=cohort,
        code="600000.SH", side=side, requested_quantity=quantity,
        reference_price="10", reason="test",
    )


def test_capacity_is_shared_across_same_code_and_rejections_stay_in_denominator():
    ledger = ManualResearchLedger(100_000, cost_scenario="baseline")
    previous = {**BAR, "volume": 10_000}  # one percent = 100 shares total
    first = ledger.execute_intent(
        _intent("intent-a", "cohort-a", "buy", 100), bar=BAR,
        previous_bar=previous, historically_eligible=True,
    )
    second = ledger.execute_intent(
        _intent("intent-b", "cohort-b", "buy", 100), bar=BAR,
        previous_bar=previous, historically_eligible=True,
    )
    assert first["filled_quantity"] == 100
    assert second["status"] == "rejected"
    assert second["reason"] == "shared_capacity_or_lot_constraint"
    assert ledger.capacity_fill_rate == Decimal("0.5")
    assert len(ledger.intents) == 2 and len(ledger.attempts) == 2 and len(ledger.trades) == 1


def test_sell_is_scoped_to_due_cohort_and_t_plus_one():
    ledger = ManualResearchLedger(100_000, cost_scenario="baseline")
    previous = {**BAR, "volume": 100_000}
    ledger.execute_intent(_intent("buy-old", "old", "buy", 100), bar=BAR, previous_bar=previous, historically_eligible=True)
    ledger.execute_intent(_intent("buy-new", "new", "buy", 100), bar=BAR, previous_bar=previous, historically_eligible=True)
    locked = ledger.execute_intent(
        _intent("sell-locked", "old", "sell", 100, day=DAY1, phase="close"),
        bar=BAR, previous_bar=previous, historically_eligible=True,
    )
    assert locked["reason"] == "cohort_t_plus_one_or_position_unavailable"
    sold = ledger.execute_intent(
        _intent("sell-old", "old", "sell", 100, day=DAY2, phase="close"),
        bar=BAR, previous_bar=previous, historically_eligible=True,
    )
    assert sold["status"] == "filled"
    assert ("old", "600000.SH") not in ledger.lots
    assert ledger.lots[("new", "600000.SH")].quantity == 100


def test_historical_eligibility_and_limit_state_fail_closed():
    ledger = ManualResearchLedger(100_000, cost_scenario="stress")
    rejected = ledger.execute_intent(
        _intent("ineligible", "cohort-a", "buy", 100), bar=BAR,
        previous_bar=BAR, historically_eligible=False,
    )
    assert rejected["reason"] == "historically_ineligible"
    limit_bar = {**BAR, "open": 11}
    limited = ledger.execute_intent(
        _intent("limit-up", "cohort-b", "buy", 100), bar=limit_bar,
        previous_bar=BAR, historically_eligible=True,
    )
    assert limited["reason"] == "upper_limit"


def test_position_snapshot_keeps_cohort_totals_equal_to_account_total():
    ledger = ManualResearchLedger(100_000, cost_scenario="baseline")
    previous = {**BAR, "volume": 100_000}
    ledger.execute_intent(_intent("buy-a", "a", "buy", 100), bar=BAR, previous_bar=previous, historically_eligible=True)
    ledger.execute_intent(_intent("buy-b", "b", "buy", 100), bar=BAR, previous_bar=previous, historically_eligible=True)
    daily = ledger.mark_close(DAY1, {"600000.SH": BAR})
    assert sum(row["quantity"] for row in ledger.positions) == 200
    assert Decimal(daily["market_value"]) == Decimal("2000.00")
    assert Decimal(daily["cash"]) + Decimal(daily["market_value"]) == Decimal(daily["equity"])
