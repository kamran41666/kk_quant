"""Evidence-gated strategy release and ManualDailyPromotionPolicy v1."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from decimal import Decimal
from typing import Any, Mapping
import hashlib
import json

from sqlalchemy.orm import Session

from server.models.schema import StrategyRelease, manual_now_str, uuid4_str


class PromotionError(ValueError):
    pass


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ManualDailyPromotionPolicyV1:
    protocol_version: str = "manual-daily-promotion-v1"
    minimum_paper_days: int = 30
    minimum_holdout_accesses: int = 1
    minimum_stress_sharpe: Decimal = Decimal("0.8")
    maximum_stress_drawdown: Decimal = Decimal("0.20")
    maximum_annual_turnover: Decimal = Decimal("226.8")
    minimum_capacity_fill_rate: Decimal = Decimal("0.95")

    def __post_init__(self) -> None:
        for name in ("minimum_stress_sharpe", "maximum_stress_drawdown", "maximum_annual_turnover", "minimum_capacity_fill_rate"):
            value = getattr(self, name)
            if not isinstance(value, Decimal):
                value = Decimal(str(value))
            if not value.is_finite() or value < 0:
                raise PromotionError(f"{name}_must_be_nonnegative_finite")
            object.__setattr__(self, name, value)

    def as_dict(self) -> dict[str, Any]:
        return {key: (str(value) if isinstance(value, Decimal) else value) for key, value in asdict(self).items()}

    @property
    def policy_hash(self) -> str:
        return _hash(self.as_dict())


def evaluate_promotion(evidence: Mapping[str, Any], policy: ManualDailyPromotionPolicyV1 | None = None) -> dict[str, Any]:
    policy = policy or ManualDailyPromotionPolicyV1()
    research = evidence.get("research", {})
    portfolio = evidence.get("portfolio", {})
    holdout = evidence.get("holdout", {})
    paper = evidence.get("paper", {})
    checks = {
        "research_hashes_complete": bool(research.get("hashes_complete")),
        "training_passed": research.get("training_decision") == "training_passed",
        "validation_passed": research.get("validation_decision") == "validation_passed",
        "no_unresolved_p0_p1": int(research.get("open_p0", 1)) == 0 and int(research.get("open_p1", 1)) == 0,
        "baseline_net_return_positive": Decimal(str(portfolio.get("baseline_net_return", "-1"))) > 0,
        "stress_net_return_positive": Decimal(str(portfolio.get("stress_net_return", "-1"))) > 0,
        "baseline_excess_positive": Decimal(str(portfolio.get("baseline_excess_return", "-1"))) > 0,
        "stress_excess_positive": Decimal(str(portfolio.get("stress_excess_return", "-1"))) > 0,
        "stress_sharpe_gate": Decimal(str(portfolio.get("stress_sharpe", "-1"))) >= policy.minimum_stress_sharpe,
        "stress_drawdown_gate": Decimal(str(portfolio.get("stress_max_drawdown", "1"))) <= policy.maximum_stress_drawdown,
        "turnover_registered": Decimal(str(portfolio.get("annual_turnover", "999999"))) <= policy.maximum_annual_turnover,
        "capacity_gate": Decimal(str(portfolio.get("capacity_fill_rate", "0"))) >= policy.minimum_capacity_fill_rate,
        "replay_exact": bool(portfolio.get("replay_exact")),
        "holdout_open_access": holdout.get("status") in {"opened", "completed"} and int(holdout.get("access_count", 0)) >= policy.minimum_holdout_accesses,
        "paper_observation_gate": int(paper.get("days", 0)) >= policy.minimum_paper_days and bool(paper.get("all_reconciled")) and bool(paper.get("no_p0_p1")),
    }
    failed = [name for name, passed in checks.items() if not passed]
    return {"policy_hash": policy.policy_hash, "checks": checks, "passed": not failed, "failed": failed}


def create_strategy_release(
    db: Session,
    *,
    strategy_key: str,
    version: str,
    bundle_hash: str,
    strategy_fingerprint: str,
    research_evidence: Mapping[str, Any],
    execution_policy: Mapping[str, Any],
    risk_policy: Mapping[str, Any],
    promotion_policy: ManualDailyPromotionPolicyV1 | Mapping[str, Any] | None = None,
) -> StrategyRelease:
    policy = promotion_policy if isinstance(promotion_policy, ManualDailyPromotionPolicyV1) else ManualDailyPromotionPolicyV1(**dict(promotion_policy or {}))
    if any(len(str(value)) != 64 for value in (bundle_hash, strategy_fingerprint)):
        raise PromotionError("release_bundle_and_strategy_fingerprint_must_be_sha256")
    if not strategy_key.strip() or not version.strip():
        raise PromotionError("release_identity_required")
    promotion_json = policy.as_dict()
    release_identity = {
        "strategy_key": strategy_key, "version": version, "bundle_hash": bundle_hash,
        "strategy_fingerprint": strategy_fingerprint, "market": "a-share",
        "research_evidence": dict(research_evidence), "execution_policy": dict(execution_policy),
        "risk_policy": dict(risk_policy), "promotion_policy": promotion_json,
    }
    row = StrategyRelease(
        id=uuid4_str(), version=version, strategy_key=strategy_key, bundle_hash=bundle_hash,
        release_hash=_hash(release_identity), strategy_fingerprint=strategy_fingerprint,
        research_evidence=_canonical(research_evidence), execution_policy=_canonical(execution_policy),
        risk_policy=_canonical(risk_policy), promotion_policy=_canonical(promotion_json), status="draft",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def promote_release(
    db: Session,
    release_id: str,
    *,
    target_status: str,
    evidence: Mapping[str, Any],
    approved_by: str | None = None,
) -> StrategyRelease:
    row = db.get(StrategyRelease, release_id)
    if row is None:
        raise PromotionError("strategy_release_not_found")
    order = {"draft": 0, "research_blocked": 1, "research_passed": 2, "portfolio_passed": 3, "holdout_passed": 4, "paper_observing": 5, "paper_passed": 6, "manual_ready": 7}
    if target_status not in order and target_status not in {"suspended", "retired"}:
        raise PromotionError("unsupported_release_status")
    if target_status == "manual_ready":
        policy = ManualDailyPromotionPolicyV1(**json.loads(row.promotion_policy or "{}"))
        result = evaluate_promotion(evidence, policy)
        if not result["passed"]:
            row.status = "research_blocked" if not result["checks"]["training_passed"] or not result["checks"]["validation_passed"] else "portfolio_passed"
            row.updated_at = manual_now_str()
            db.commit()
            raise PromotionError(f"manual_ready_gates_failed:{','.join(result['failed'])}")
        if not approved_by:
            raise PromotionError("manual_ready_approval_actor_required")
    elif target_status in order and target_status < order.get(row.status, 0):
        raise PromotionError("release_status_cannot_move_backwards")
    row.status = target_status
    if target_status == "manual_ready":
        row.approved_by = approved_by
        row.approved_at = manual_now_str()
    row.updated_at = manual_now_str()
    db.commit()
    db.refresh(row)
    return row


__all__ = ["ManualDailyPromotionPolicyV1", "PromotionError", "evaluate_promotion", "create_strategy_release", "promote_release"]
