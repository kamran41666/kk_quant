"""Evidence-gated strategy release and ManualDailyPromotionPolicy v1."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping
import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from server.models.schema import StrategyRelease, manual_now_str, uuid4_str


class PromotionError(ValueError):
    pass


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _metric(value: Any, field: str, *, minimum: Decimal | None = None, maximum: Decimal | None = None) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise PromotionError(f"{field}_must_be_numeric") from exc
    if not result.is_finite():
        raise PromotionError(f"{field}_must_be_finite")
    if minimum is not None and result < minimum:
        raise PromotionError(f"{field}_below_minimum")
    if maximum is not None and result > maximum:
        raise PromotionError(f"{field}_above_maximum")
    return result


def _count(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise PromotionError(f"{field}_must_be_integer")
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise PromotionError(f"{field}_must_be_integer") from exc
    if str(value).strip() != str(result) and not isinstance(value, int):
        raise PromotionError(f"{field}_must_be_integer")
    if result < 0:
        raise PromotionError(f"{field}_must_be_nonnegative")
    return result


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
    open_p0 = _count(research.get("open_p0", 1), "open_p0")
    open_p1 = _count(research.get("open_p1", 1), "open_p1")
    baseline_net = _metric(portfolio.get("baseline_net_return", "-1"), "baseline_net_return")
    stress_net = _metric(portfolio.get("stress_net_return", "-1"), "stress_net_return")
    baseline_excess = _metric(portfolio.get("baseline_excess_return", "-1"), "baseline_excess_return")
    stress_excess = _metric(portfolio.get("stress_excess_return", "-1"), "stress_excess_return")
    stress_sharpe = _metric(portfolio.get("stress_sharpe", "-1"), "stress_sharpe")
    # The promotion protocol stores drawdown as a non-negative magnitude.
    # Reject negative returns-style values instead of letting -95% satisfy a
    # <=20% magnitude gate.
    stress_drawdown = _metric(
        portfolio.get("stress_max_drawdown", "1"),
        "stress_max_drawdown", minimum=Decimal("0"), maximum=Decimal("1"),
    )
    annual_turnover = _metric(portfolio.get("annual_turnover", "999999"), "annual_turnover", minimum=Decimal("0"))
    capacity_fill_rate = _metric(
        portfolio.get("capacity_fill_rate", "0"),
        "capacity_fill_rate", minimum=Decimal("0"), maximum=Decimal("1"),
    )
    access_count = _count(holdout.get("access_count", 0), "holdout_access_count")
    paper_days = _count(paper.get("days", 0), "paper_days")
    checks = {
        "research_hashes_complete": bool(research.get("hashes_complete")),
        "training_passed": research.get("training_decision") == "training_passed",
        "validation_passed": research.get("validation_decision") == "validation_passed",
        "no_unresolved_p0_p1": open_p0 == 0 and open_p1 == 0,
        "baseline_net_return_positive": baseline_net > 0,
        "stress_net_return_positive": stress_net > 0,
        "baseline_excess_positive": baseline_excess > 0,
        "stress_excess_positive": stress_excess > 0,
        "stress_sharpe_gate": stress_sharpe >= policy.minimum_stress_sharpe,
        "stress_drawdown_gate": stress_drawdown <= policy.maximum_stress_drawdown,
        "turnover_registered": annual_turnover <= policy.maximum_annual_turnover,
        "capacity_gate": capacity_fill_rate >= policy.minimum_capacity_fill_rate,
        "replay_exact": bool(portfolio.get("replay_exact")),
        "holdout_open_access": holdout.get("status") == "completed" and access_count >= policy.minimum_holdout_accesses,
        "paper_observation_gate": paper_days >= policy.minimum_paper_days and bool(paper.get("all_reconciled")) and bool(paper.get("no_p0_p1")),
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
    existing = db.scalars(select(StrategyRelease).where(
        StrategyRelease.release_hash == row.release_hash,
    )).first()
    if existing is not None:
        return existing
    conflicting = db.scalars(select(StrategyRelease).where(
        StrategyRelease.strategy_key == strategy_key,
        StrategyRelease.version == version,
    )).first()
    if conflicting is not None:
        raise PromotionError("strategy_release_version_conflict")
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
    if target_status not in {"suspended", "retired"}:
        raise PromotionError("legacy_promotion_path_disabled_use_evidence_resolver")
    row = db.get(StrategyRelease, release_id)
    if row is None:
        raise PromotionError("strategy_release_not_found")
    order = {"draft": 0, "research_blocked": 1, "research_passed": 2, "portfolio_passed": 3, "holdout_passed": 4, "paper_observing": 5, "paper_passed": 6, "manual_ready": 7}
    if target_status not in order and target_status not in {"suspended", "retired"}:
        raise PromotionError("unsupported_release_status")
    if target_status == "manual_ready":
        if row.status != "paper_passed":
            raise PromotionError("manual_ready_requires_paper_passed_release")
        frozen_evidence = json.loads(row.research_evidence or "{}")
        if _hash(frozen_evidence) != _hash(dict(evidence)):
            raise PromotionError("promotion_evidence_does_not_match_frozen_release")
        policy = ManualDailyPromotionPolicyV1(**json.loads(row.promotion_policy or "{}"))
        result = evaluate_promotion(evidence, policy)
        if not result["passed"]:
            row.status = "research_blocked" if not result["checks"]["training_passed"] or not result["checks"]["validation_passed"] else "portfolio_passed"
            row.updated_at = manual_now_str()
            db.commit()
            raise PromotionError(f"manual_ready_gates_failed:{','.join(result['failed'])}")
        if not approved_by:
            raise PromotionError("manual_ready_approval_actor_required")
    elif target_status in order:
        allowed = {
            "draft": {"research_blocked", "research_passed"},
            "research_blocked": set(),
            "research_passed": {"portfolio_passed"},
            "portfolio_passed": {"holdout_passed"},
            "holdout_passed": {"paper_observing"},
            "paper_observing": {"paper_passed"},
            "paper_passed": {"manual_ready"},
            "manual_ready": set(),
        }
        if target_status != row.status and target_status not in allowed.get(row.status, set()):
            raise PromotionError("release_status_transition_not_allowed")
    row.status = target_status
    if target_status == "manual_ready":
        row.approved_by = approved_by
        row.approved_at = manual_now_str()
    row.updated_at = manual_now_str()
    db.commit()
    db.refresh(row)
    return row


__all__ = ["ManualDailyPromotionPolicyV1", "PromotionError", "evaluate_promotion", "create_strategy_release", "promote_release"]
