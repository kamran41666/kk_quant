"""Cohort and capacity invariants for the H2b research execution ledger."""
from datetime import date
from decimal import Decimal

import pytest
import pandas as pd

from quant_engine.backtest.manual_research_ledger import (
    ManualResearchLedger,
    ResearchCohortLot,
    ResearchCorporateAction,
    ResearchExecutionIntent,
)
from quant_engine.backtest.manual_research_inputs import load_research_corporate_actions


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


def test_record_date_entitlement_survives_sale_and_cash_moves_via_receivable():
    ledger = ManualResearchLedger(100_000, cost_scenario="baseline")
    previous = {**BAR, "volume": 1_000_000}
    ledger.execute_intent(_intent("buy-a-action", "a", "buy", 100), bar=BAR, previous_bar=previous, historically_eligible=True)
    ledger.execute_intent(_intent("buy-b-action", "b", "buy", 200), bar=BAR, previous_bar=previous, historically_eligible=True)
    action = ResearchCorporateAction(
        action_id="action-1", code="600000.SH", record_date=date(2027, 1, 5),
        ex_date=date(2027, 1, 7), pay_date=date(2027, 1, 8),
        stock_listing_date=date(2027, 1, 11), source_hash="a" * 64,
        cash_per_share="0.5", bonus_ratio="0.1",
    )
    ledger.register_corporate_actions([action])
    ledger.record_action_entitlements_at_close(date(2027, 1, 5))
    ledger.execute_intent(
        _intent("sell-a-before-ex", "a", "sell", 100, day=date(2027, 1, 6), phase="close"),
        bar=BAR, previous_bar=previous, historically_eligible=True,
    )
    assert ("a", "600000.SH") not in ledger.lots
    cash_before_ex = ledger.cash
    ledger.process_corporate_actions_at_open(date(2027, 1, 7))
    action_event_count = len(ledger.corporate_actions)
    ledger.process_corporate_actions_at_open(date(2027, 1, 7))
    assert len(ledger.corporate_actions) == action_event_count
    assert ledger.cash == cash_before_ex
    assert ledger.receivable_cash == Decimal("150.00")
    assert ledger.lots[("a", "600000.SH")].quantity == 10
    assert ledger.lots[("b", "600000.SH")].quantity == 220
    assert ledger.lots[("a", "600000.SH")].sellable(date(2027, 1, 8)) == 0
    ex_equity = Decimal(ledger.mark_close(date(2027, 1, 7), {"600000.SH": BAR})["equity"])
    ledger.process_corporate_actions_at_open(date(2027, 1, 8))
    assert ledger.receivable_cash == 0
    assert ledger.cash == cash_before_ex + Decimal("150.00")
    paid_event_count = len(ledger.corporate_actions)
    ledger.process_corporate_actions_at_open(date(2027, 1, 8))
    assert len(ledger.corporate_actions) == paid_event_count
    pay_equity = Decimal(ledger.mark_close(date(2027, 1, 8), {"600000.SH": BAR})["equity"])
    assert pay_equity == ex_equity
    ledger.process_corporate_actions_at_open(date(2027, 1, 11))
    assert ledger.lots[("a", "600000.SH")].sellable(date(2027, 1, 11)) == 10


def test_bonus_and_cash_are_rounded_at_account_then_stably_allocated():
    ledger = ManualResearchLedger(100_000, cost_scenario="baseline")
    previous = {**BAR, "volume": 1_000_000}
    ledger.execute_intent(_intent("buy-a-round", "a", "buy", 100), bar=BAR, previous_bar=previous, historically_eligible=True)
    ledger.execute_intent(_intent("buy-b-round", "b", "buy", 100), bar=BAR, previous_bar=previous, historically_eligible=True)
    action = ResearchCorporateAction(
        action_id="action-round", code="600000.SH", record_date=date(2027, 1, 5),
        ex_date=date(2027, 1, 6), pay_date=date(2027, 1, 7),
        stock_listing_date=date(2027, 1, 7), source_hash="b" * 64,
        cash_per_share="0.00005", bonus_ratio="0.005",
    )
    ledger.register_corporate_actions([action])
    ledger.record_action_entitlements_at_close(date(2027, 1, 5))
    ledger.process_corporate_actions_at_open(date(2027, 1, 6))
    bonuses = [row for row in ledger.corporate_actions if row["stage"] == "ex_bonus"]
    cash = [row for row in ledger.corporate_actions if row["stage"] == "ex_cash"]
    assert sum(row["bonus_quantity"] for row in bonuses) == 1
    assert [(row["cohort_id"], row["bonus_quantity"]) for row in bonuses] == [("a", 1)]
    assert sum(Decimal(row["receivable_cash"]) for row in cash) == Decimal("0.01")
    assert [(row["cohort_id"], row["receivable_cash"]) for row in cash] == [("a", "0.01"), ("b", "0.00")]


def test_unresolved_action_fields_block_evidence_without_creating_rights():
    ledger = ManualResearchLedger(100_000, cost_scenario="baseline")
    action = ResearchCorporateAction(
        action_id="action-unresolved", code="600000.SH", record_date=None,
        ex_date=date(2027, 1, 6), pay_date=None, stock_listing_date=None,
        source_hash="c" * 64, cash_per_share="0.5", bonus_ratio="0.1",
        allocation_verified=False,
    )
    ledger.register_corporate_actions([action, action])
    ledger.process_corporate_actions_at_open(date(2027, 1, 6))
    assert ledger.company_action_evidence_valid is False
    assert ledger.receivable_cash == 0
    assert ledger.lots == {}
    reasons = {item["reason"] for item in ledger.corporate_action_audit}
    assert {"record_date_unresolved", "pay_date_unresolved", "stock_listing_date_unresolved", "bonus_allocation_unverified", "record_entitlements_missing"} <= reasons


def test_duplicate_economic_action_is_deduplicated_and_conflicting_source_is_rejected():
    ledger = ManualResearchLedger(100_000, cost_scenario="baseline")
    common = dict(
        code="600000.SH", record_date=date(2027, 1, 5), ex_date=date(2027, 1, 6),
        pay_date=date(2027, 1, 7), stock_listing_date=None,
        cash_per_share="0.5", bonus_ratio="0",
    )
    first = ResearchCorporateAction(action_id="source-a", source_hash="d" * 64, **common)
    duplicate = ResearchCorporateAction(action_id="source-b", source_hash="d" * 64, **common)
    ledger.register_corporate_actions([first, duplicate])
    assert len(ledger._actions) == 1
    conflict = ResearchCorporateAction(action_id="source-c", source_hash="e" * 64, **common)
    with pytest.raises(ValueError, match="duplicate_economic_action_conflict"):
        ledger.register_corporate_actions([conflict])


def test_invalid_pay_date_never_becomes_early_buying_power():
    ledger = ManualResearchLedger(100_000, cost_scenario="baseline")
    previous = {**BAR, "volume": 1_000_000}
    ledger.execute_intent(_intent("buy-invalid-pay", "a", "buy", 100), bar=BAR, previous_bar=previous, historically_eligible=True)
    action = ResearchCorporateAction(
        action_id="invalid-pay", code="600000.SH", record_date=date(2027, 1, 5),
        ex_date=date(2027, 1, 7), pay_date=date(2027, 1, 6),
        stock_listing_date=None, source_hash="f" * 64,
        cash_per_share="0.5", bonus_ratio="0",
    )
    ledger.register_corporate_actions([action])
    ledger.record_action_entitlements_at_close(date(2027, 1, 5))
    cash_before = ledger.cash
    ledger.process_corporate_actions_at_open(date(2027, 1, 7))
    assert ledger.cash == cash_before
    assert ledger.receivable_cash == Decimal("50.00")
    assert ledger.receivables[0]["pay_date"] is None
    assert ledger.company_action_evidence_valid is False
    ledger.process_corporate_actions_at_open(date(2027, 1, 8))
    assert ledger.cash == cash_before


def test_normalized_action_adapter_has_stable_ids_and_preserves_invalid_dates():
    frame = pd.DataFrame([{
        "code": "002358.SZ", "record_date": "2019-07-03", "ex_date": "2019-07-05",
        "pay_date": "2019-07-04", "stock_date": None, "cash_ps": "0.1",
        "bonus_ratio": "0", "bonus_allocation_verified": True,
    }])
    first = load_research_corporate_actions(frame, source_hash="1" * 64)
    second = load_research_corporate_actions(frame.copy(), source_hash="1" * 64)
    assert first == second
    assert first[0].action_id == second[0].action_id
    assert first[0].pay_date < first[0].ex_date


def test_sell_fees_use_final_sellable_quantity_after_bonus_lock():
    ledger = ManualResearchLedger(100_000, cost_scenario="baseline")
    ledger.lots[("locked", "600000.SH")] = ResearchCohortLot(
        "locked", "600000.SH", 150, date(2027, 1, 4), Decimal("10"),
        Decimal("1500"), [(date(2027, 1, 8), 50)],
    )
    attempt = ledger.execute_intent(
        _intent("sell-unlocked-only", "locked", "sell", 150, day=date(2027, 1, 5), phase="close"),
        bar=BAR, previous_bar={**BAR, "volume": 1_000_000}, historically_eligible=True,
    )
    trade = ledger.trades[-1]
    assert attempt["filled_quantity"] == 100
    assert Decimal(trade["gross"]) == Decimal("999.00")
    assert Decimal(trade["transfer_fee"]) == Decimal("0.02")
    assert Decimal(trade["stamp_duty"]) == Decimal("0.50")
    assert Decimal(trade["total_fee"]) == Decimal("5.52")
