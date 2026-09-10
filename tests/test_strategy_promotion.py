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


def test_policy_fails_closed_on_missing_evidence():
    result = evaluate_promotion({"research": {"training_decision": "training_passed"}})
    assert result["passed"] is False
    assert "stress_net_return_positive" in result["failed"]


def test_legacy_release_promotion_path_is_disabled(db_session):
    release = create_strategy_release(
        db_session, strategy_key="manual-test", version="v2", bundle_hash="1" * 64,
        strategy_fingerprint="2" * 64, research_evidence=_evidence(),
        execution_policy={"auto_submit": False}, risk_policy={"max_drawdown": "0.2"},
    )
    with pytest.raises(PromotionError, match="evidence_resolver"):
        promote_release(db_session, release.id, target_status="manual_ready", evidence=_evidence(), approved_by="operator")
    assert db_session.get(type(release), release.id).status == "draft"


def test_policy_hash_is_stable():
    assert ManualDailyPromotionPolicyV1().policy_hash == ManualDailyPromotionPolicyV1().policy_hash


def test_drawdown_magnitude_is_strict():
    invalid = _evidence()
    invalid["portfolio"]["stress_max_drawdown"] = "-0.95"
    with pytest.raises(PromotionError, match="stress_max_drawdown_below_minimum"):
        evaluate_promotion(invalid)
