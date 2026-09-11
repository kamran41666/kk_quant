"""Pure engineering runtime tests; no server or database is involved."""
from __future__ import annotations

import json
from decimal import Decimal

import pytest

from quant_engine.trading.manual_daily_runtime import (
    advance_demo_state,
    create_demo_state,
    replay_daily_cycle,
)


def _to_plan(state):
    for action in ("research", "observe", "plan"):
        state = advance_demo_state(state, action)
    return state


def test_state_shape_and_strict_action_order():
    state = create_demo_state(seed=7)
    assert state["stage"] == "created" and state["next_action"] == "research"
    assert state["mode"] == "engineering_demo"
    with pytest.raises(ValueError, match="invalid_demo_action"):
        advance_demo_state(state, "plan")
    state = _to_plan(state)
    assert state["stage"] == "planned" and state["next_action"] == "confirm"
    assert {"open", "close"} == {plan["phase"] for plan in state["plans"]}
    assert all(item["reference_price"] is not None for plan in state["plans"] for item in plan["items"])
    assert all(item["reference_price"] != state["market"]["bars"][item["code"]][state["current_date"]]["open"] for plan in state["plans"] for item in plan["items"] if item["phase"] == "open")
    frozen = json.dumps(state["plans"], sort_keys=True)
    state["market"]["bars"][state["plans"][1]["items"][0]["code"]][state["current_date"]]["open"] = "9999.99"
    assert json.dumps(state["plans"], sort_keys=True) == frozen


def test_multiday_rolls_cash_positions_and_does_not_mutate_old_state():
    original = create_demo_state(seed=7)
    state = _to_plan(original)
    confirmed = advance_demo_state(state, "confirm")
    filled = advance_demo_state(confirmed, "fill", fill_mode="full")
    reviewed = advance_demo_state(filled, "review")
    next_plan = advance_demo_state(reviewed, "next_day")
    assert original["stage"] == "created"
    assert len(reviewed["nav"]) == 1 and reviewed["current_date"] == reviewed["nav"][0]["date"]
    assert reviewed["reviews"][0]["daily_return"] == str(Decimal(reviewed["equity"]) / Decimal(reviewed["capital"]) - 1)
    assert next_plan["current_date"] > reviewed["current_date"]
    assert next_plan["cash"] != "1000000.00"
    assert all(float(item["quantity"]) > 0 for item in reviewed["positions"])
    assert Decimal(next_plan["cash"]) <= Decimal(next_plan["capital"])
    second = advance_demo_state(next_plan, "confirm")
    second = advance_demo_state(second, "fill", fill_mode="partial")
    assert any(item["phase"] == "close" and item["status"] == "partially_filled" for item in second["fills"])
    second_review = advance_demo_state(second, "review")
    retry_plan = advance_demo_state(second_review, "next_day")
    assert any(item["phase"] == "close" and item["cohort_id"] == second["fills"][0]["cohort_id"] for plan in retry_plan["plans"] for item in plan["items"])


def test_partial_and_unfilled_are_recorded_without_fabricating_cash():
    state = _to_plan(create_demo_state())
    state = advance_demo_state(state, "confirm")
    partial = advance_demo_state(state, "fill", fill_mode="partial")
    assert any(item["status"] == "partially_filled" for item in partial["fills"])
    partial_cash = Decimal(partial["cash"])
    reviewed = advance_demo_state(partial, "review")
    next_plan = advance_demo_state(reviewed, "next_day")
    next_plan = advance_demo_state(next_plan, "confirm")
    unfilled = advance_demo_state(next_plan, "fill", fill_mode="unfilled")
    assert any(item["status"] == "unfilled" and item["unfilled_reason"] for item in unfilled["fills"])
    assert Decimal(unfilled["cash"]) >= partial_cash


def test_replay_is_deterministic_and_observation_is_engineering_only():
    actions = ["research", "observe", "plan", "confirm", "fill", "review", "next_day"]
    first = replay_daily_cycle([{"action": action, "fill_mode": "full"} for action in actions], {"seed": 7})
    second = replay_daily_cycle([{"action": action, "fill_mode": "full"} for action in actions], {"seed": 7})
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert first["observation"]["status"] == "engineering_completed"
    assert first["observation"]["eligible_for_real"] is False
    assert first["research"]["curves"] and {"date", "strategy", "hs300", "shanghai"} <= set(first["research"]["curves"][0])
    assert {"total_return", "max_drawdown", "sharpe", "trade_count"} <= set(first["research"]["metrics"])
    with pytest.raises(ValueError, match="opaque_state"):
        replay_daily_cycle(first)
