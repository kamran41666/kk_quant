"""M4 evidence-gated release tests."""
import pytest

from server.services.strategy_promotion import (
    ManualDailyPromotionPolicyV1,
    PromotionError,
    create_strategy_release,
    evaluate_promotion,
    promote_release,
)


def _evidence():
    return {
        "research": {"hashes_complete": True, "training_decision": "training_passed", "validation_decision": "validation_passed", "open_p0": 0, "open_p1": 0},
        "portfolio": {"baseline_net_return": "0.10", "stress_net_return": "0.03", "baseline_excess_return": "0.05", "stress_excess_return": "0.01", "stress_sharpe": "0.9", "stress_max_drawdown": "0.18", "annual_turnover": "100", "capacity_fill_rate": "0.98", "replay_exact": True},
        "holdout": {"status": "completed", "access_count": 1},
        "paper": {"days": 30, "all_reconciled": True, "no_p0_p1": True},
    }


def _advance_to_paper_passed(db, release):
    for status in ("research_passed", "portfolio_passed", "holdout_passed", "paper_observing", "paper_passed"):
        release = promote_release(db, release.id, target_status=status, evidence={})
    return release


def test_policy_fails_closed_on_missing_evidence():
    result = evaluate_promotion({"research": {"training_decision": "training_passed"}})
    assert result["passed"] is False
    assert "stress_net_return_positive" in result["failed"]


def test_release_requires_all_gates_and_is_not_legacy_inherited(db_session):
    release = create_strategy_release(
        db_session, strategy_key="manual-test", version="v2", bundle_hash="1" * 64,
        strategy_fingerprint="2" * 64, research_evidence=_evidence(),
        execution_policy={"auto_submit": False}, risk_policy={"max_drawdown": "0.2"},
    )
    with pytest.raises(PromotionError, match="does_not_match"):
        _advance_to_paper_passed(db_session, release)
        promote_release(db_session, release.id, target_status="manual_ready", evidence={"research": _evidence()["research"], "portfolio": {}, "holdout": {}, "paper": {}}, approved_by="operator")
    promoted = promote_release(db_session, release.id, target_status="manual_ready", evidence=_evidence(), approved_by="operator")
    assert promoted.status == "manual_ready"
    assert promoted.approved_by == "operator"


def test_policy_hash_is_stable():
    assert ManualDailyPromotionPolicyV1().policy_hash == ManualDailyPromotionPolicyV1().policy_hash


def test_intermediate_status_and_drawdown_magnitude_are_strict(db_session):
    release = create_strategy_release(
        db_session, strategy_key="manual-stage", version="v1", bundle_hash="3" * 64,
        strategy_fingerprint="4" * 64, research_evidence=_evidence(),
        execution_policy={"auto_submit": False}, risk_policy={"max_drawdown": "0.2"},
    )
    assert promote_release(db_session, release.id, target_status="research_passed", evidence={}).status == "research_passed"
    with pytest.raises(PromotionError, match="transition_not_allowed"):
        promote_release(db_session, release.id, target_status="holdout_passed", evidence={})
    invalid = _evidence()
    invalid["portfolio"]["stress_max_drawdown"] = "-0.95"
    with pytest.raises(PromotionError, match="stress_max_drawdown_below_minimum"):
        evaluate_promotion(invalid)
