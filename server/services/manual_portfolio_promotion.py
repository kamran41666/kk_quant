"""Evidence resolver for the manual-daily portfolio promotion gate.

The resolver accepts only the two database artifact ids.  Registration and
read-back remain responsible for the persisted artifact and independent
accounting/source replay checks; this module adds the release-chain checks,
promotion metrics, and a separate deterministic execution-protocol replay.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import fields
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from quant_engine.backtest.manual_daily_portfolio_v3 import (
    run_manual_daily_portfolio_v3,
)
from quant_engine.backtest.manual_portfolio_artifacts import (
    reconstruct_result,
    run_envelope,
)
from quant_engine.backtest.manual_portfolio_evidence import _table
from quant_engine.backtest.manual_portfolio_sources import (
    load_verified_portfolio_sources,
)
from server.config import settings
from server.models.schema import (
    ResearchEvidenceArtifact,
    StrategyPromotionEvaluation,
    StrategyRelease,
)
from server.services.strategy_promotion import ManualDailyPromotionPolicyV1


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)


def _hash(value: Any) -> str:
    import hashlib

    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field}_must_be_numeric") from exc
    if not result.is_finite():
        raise ValueError(f"{field}_must_be_finite")
    return result


def _json_object(value: str, field: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"{field}_invalid") from exc
    if not isinstance(parsed, dict):
        raise TypeError(f"{field}_invalid")
    return parsed


def _artifact_tables(result: Any) -> dict[str, Any]:
    rows = {
        "signals.parquet": result.signals,
        "order_intents.parquet": result.intents,
        "order_attempts.parquet": result.attempts,
        "trades.parquet": result.trades,
        "corporate_actions.parquet": result.corporate_actions,
        "positions.parquet": result.positions,
        "daily_portfolio.parquet": result.daily,
        "benchmark.parquet": result.benchmark,
    }
    # Importing this mapping through the evidence writer keeps the replay
    # normalization exactly aligned with the persisted Arrow contract.
    from quant_engine.backtest.manual_portfolio_evidence import _ARROW_SCHEMAS

    return {name: _table(rows[name], schema) for name, schema in _ARROW_SCHEMAS.items()}


def _reproduce_execution_protocol(artifact: Any, source_root: Path) -> tuple[bool, str | None]:
    """Re-run the frozen generator and compare its persisted-form result hash.

    This is deliberately separate from the artifact reader's accounting/source
    replay.  It checks that the source inputs, cohort budget and execution
    timing deterministically regenerate the registered result.
    """
    manifest = artifact.manifest
    locator = manifest["source_locator"]
    receipts = locator["receipts"]
    receipt_paths = {}
    for item in receipts:
        path = Path(item["path"])
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("portfolio_source_locator_path_invalid")
        receipt_paths[item["role"]] = source_root / path
    input_manifest = artifact.result.input_manifest
    signal_item = next(item for item in input_manifest.source_files if item.get("role") == "signals")
    signal_path = Path(signal_item["path"])
    if signal_path.is_absolute() or ".." in signal_path.parts:
        raise ValueError("portfolio_signal_locator_path_invalid")
    sources = load_verified_portfolio_sources(
        dataset_manifest_path=receipt_paths["dataset_manifest"],
        benchmark_receipt_path=receipt_paths["benchmark_receipt"],
        signal_path=source_root / signal_path,
        signal_sha256=input_manifest.signal_content_hash,
        training_artifact_id=input_manifest.training_artifact_id,
        training_artifact_hash=input_manifest.training_artifact_hash,
        validation_artifact_id=input_manifest.validation_artifact_id,
        validation_artifact_hash=input_manifest.validation_artifact_hash,
        allowed_root=source_root,
        signal_value_column=locator["signal_value_column"],
    )
    if _canonical(sources.input_manifest.__dict__) != _canonical(input_manifest.__dict__):
        raise ValueError("portfolio_reproduced_input_manifest_mismatch")
    if _canonical(sources.source_locator()) != _canonical(locator) or not sources.verify_files():
        raise ValueError("portfolio_reproduced_source_locator_mismatch")
    fresh = run_manual_daily_portfolio_v3(
        daily=sources.daily, eligibility=sources.eligibility,
        benchmark=sources.benchmark, signals=sources.signals,
        corporate_actions=sources.corporate_actions, calendar=sources.calendar,
        bundle=artifact.result.bundle, input_manifest=input_manifest,
        start=artifact.result.start_date, end=artifact.result.end_date,
        initial_capital=artifact.result.initial_capital,
    )
    audit = {
        "schema_version": "manual-portfolio-audit-v1", "items": fresh.audit,
        "quality_errors": fresh.quality_errors,
        "error_count": sum(item.get("severity") == "error" for item in fresh.audit),
        "warning_count": sum(item.get("severity") == "warning" for item in fresh.audit),
    }
    normalized = reconstruct_result(
        run_envelope(fresh), fresh.input_manifest.__dict__, _artifact_tables(fresh), audit,
    )
    if not sources.verify_files():
        raise ValueError("portfolio_source_changed_during_reproduction")
    if normalized.result_hash != artifact.result.result_hash:
        return False, normalized.result_hash
    return True, normalized.result_hash


def _research_evaluation_valid(
    db: Session, release: StrategyRelease, research_refs: Mapping[str, str], policy_hash: str,
) -> tuple[bool, str]:
    from server.services.manual_evidence import _resolve_research_passed

    rows = db.scalars(select(StrategyPromotionEvaluation).where(
        StrategyPromotionEvaluation.release_id == release.id,
        StrategyPromotionEvaluation.target_status == "research_passed",
        StrategyPromotionEvaluation.decision == "passed",
    )).all()
    if len(rows) != 1:
        raise ValueError("research_passed_evaluation_count_invalid")
    row = rows[0]
    if (
        row.from_status != "draft" or row.previous_evaluation_id is not None
        or row.previous_evaluation_hash != "" or row.release_hash != release.release_hash
        or row.policy_hash != policy_hash
    ):
        raise ValueError("research_passed_evaluation_chain_invalid")
    refs = _json_object(row.evidence_refs_json, "research_evaluation_refs")
    if refs != dict(research_refs):
        raise ValueError("research_passed_evaluation_refs_mismatch")
    checks = _json_object(row.checks_json, "research_evaluation_checks")
    if not checks or any(type(value) is not bool or not value for value in checks.values()):
        raise ValueError("research_passed_evaluation_checks_invalid")
    saved = _resolve_research_passed(db, release, research_refs)
    if not saved.passed:
        raise ValueError("research_passed_evaluation_checks_not_passed")
    if row.resolved_evidence_hash != saved.resolved_evidence_hash:
        raise ValueError("research_passed_evidence_hash_mismatch")
    request = {
        "release_id": release.id, "release_hash": release.release_hash,
        "target_status": "research_passed", "evidence_refs": dict(research_refs),
        "actor": row.actor,
    }
    if row.request_hash != _hash(request):
        raise ValueError("research_passed_request_hash_mismatch")
    evaluation_payload = {
        "release_id": release.id, "release_hash": release.release_hash,
        "from_status": row.from_status, "target_status": row.target_status,
        "policy_hash": policy_hash, "evidence_refs": dict(research_refs),
        "resolved_evidence_hash": row.resolved_evidence_hash,
        "checks": checks, "decision": row.decision,
        "resolver_version": row.resolver_version,
        # Preserve the hash that was stored on this historical evaluation.
        "resolver_code_hash": row.resolver_code_hash,
        "previous_evaluation_hash": "", "actor": row.actor,
        "request_hash": row.request_hash,
    }
    if row.evaluation_hash != _hash(evaluation_payload):
        raise ValueError("research_passed_evaluation_hash_mismatch")
    return True, row.evaluation_hash


def resolve_portfolio_passed(
    db: Session, release: StrategyRelease, evidence_refs: Mapping[str, str],
) -> Any:
    """Resolve a portfolio gate from registered artifact identities only."""
    try:
        if set(evidence_refs) != {"baseline_artifact_id", "stress_artifact_id"}:
            raise ValueError("portfolio_promotion_requires_baseline_and_stress_artifact_ids")
        from server.services.manual_portfolio_registration import (
            reverify_registered_portfolio_pair,
        )

        source_root = Path(settings.result_dir).resolve(strict=True).parent
        pair = reverify_registered_portfolio_pair(
            db, evidence_refs["baseline_artifact_id"], evidence_refs["stress_artifact_id"],
            source_root=source_root,
        )
        baseline, stress = pair["baseline"], pair["stress"]
        tracked_ids = {
            "baseline": evidence_refs["baseline_artifact_id"],
            "stress": evidence_refs["stress_artifact_id"],
            "training": baseline.result.input_manifest.training_artifact_id,
            "validation": baseline.result.input_manifest.validation_artifact_id,
        }
        tracked_rows = {
            name: db.get(ResearchEvidenceArtifact, artifact_id)
            for name, artifact_id in tracked_ids.items()
        }
        from server.services.manual_evidence import verify_research_artifact

        if any(row is None or row.status != "verified" or not verify_research_artifact(row) for row in tracked_rows.values()):
            raise ValueError("portfolio_artifact_registration_missing")
        registered_hashes = {name: (row.status, row.evidence_hash) for name, row in tracked_rows.items()}
        bundle = baseline.result.bundle
        release_execution = _json_object(release.execution_policy, "release_execution_policy")
        frozen_refs = _json_object(release.research_evidence, "release_research_evidence")
        identity = bundle.identity()["execution_policy"]
        checks: dict[str, bool] = {
            "release_identity_valid": False,
            "strategy_key_match": release.strategy_key == bundle.strategy_key,
            "bundle_hash_match": release.bundle_hash == bundle.bundle_hash,
            "strategy_fingerprint_match": release.strategy_fingerprint == bundle.strategy_core_hash,
            "execution_policy_match": release_execution == identity and release_execution.get("auto_submit") is False,
            "pair_scenarios_valid": bundle.cost_scenario == "baseline" and stress.result.bundle.cost_scenario == "stress",
            "research_refs_match": (
                frozen_refs.get("training_artifact_id") == baseline.result.input_manifest.training_artifact_id
                and frozen_refs.get("validation_artifact_id") == baseline.result.input_manifest.validation_artifact_id
                and frozen_refs.get("training_artifact_id") == stress.result.input_manifest.training_artifact_id
                and frozen_refs.get("validation_artifact_id") == stress.result.input_manifest.validation_artifact_id
                and baseline.result.input_manifest.training_artifact_hash
                == stress.result.input_manifest.training_artifact_hash
                and baseline.result.input_manifest.validation_artifact_hash
                == stress.result.input_manifest.validation_artifact_hash
            ),
        }
        from server.services.manual_evidence import _release_identity_valid

        checks["release_identity_valid"] = _release_identity_valid(release)
        policy_payload = _json_object(release.promotion_policy, "promotion_policy")
        expected_policy_fields = {field.name for field in fields(ManualDailyPromotionPolicyV1)}
        if set(policy_payload) != expected_policy_fields or policy_payload.get("protocol_version") != "manual-daily-promotion-v1":
            raise ValueError("promotion_policy_protocol_invalid")
        policy = ManualDailyPromotionPolicyV1(**policy_payload)
        if not (Decimal(0) <= policy.maximum_stress_drawdown <= Decimal(1)):
            raise ValueError("maximum_stress_drawdown_out_of_range")
        if not (Decimal(0) <= policy.minimum_capacity_fill_rate <= Decimal(1)):
            raise ValueError("minimum_capacity_fill_rate_out_of_range")
        policy_hash = policy.policy_hash
        checks["research_passed_chain_valid"], previous_hash = _research_evaluation_valid(
            db, release, frozen_refs, policy_hash,
        )
        metrics = {"baseline": baseline.metrics, "stress": stress.metrics}
        required = (
            "total_return", "excess_return", "sharpe_252_rf0", "max_drawdown_magnitude",
            "annual_turnover_double_sided", "capacity_fill_rate_amount_weighted",
        )
        parsed = {}
        for scenario, artifact in (("baseline", baseline), ("stress", stress)):
            parsed[scenario] = {field: _decimal(artifact.metrics[field], f"{scenario}_{field}") for field in required}
        checks.update({
            "baseline_total_return_positive": parsed["baseline"]["total_return"] > 0,
            "stress_total_return_positive": parsed["stress"]["total_return"] > 0,
            "baseline_excess_return_positive": parsed["baseline"]["excess_return"] > 0,
            "stress_excess_return_positive": parsed["stress"]["excess_return"] > 0,
            "stress_sharpe_gate": parsed["stress"]["sharpe_252_rf0"] >= policy.minimum_stress_sharpe,
            "stress_drawdown_gate": Decimal(0) <= parsed["stress"]["max_drawdown_magnitude"] <= Decimal(1)
            and parsed["stress"]["max_drawdown_magnitude"] <= policy.maximum_stress_drawdown,
            "turnover_range": all(Decimal(0) <= parsed[name]["annual_turnover_double_sided"] for name in ("baseline", "stress")),
            "turnover_gate": max(parsed["baseline"]["annual_turnover_double_sided"], parsed["stress"]["annual_turnover_double_sided"]) <= policy.maximum_annual_turnover,
            "fill_rate_range": all(Decimal(0) <= parsed[name]["capacity_fill_rate_amount_weighted"] <= Decimal(1) for name in ("baseline", "stress")),
            "fill_rate_gate": min(parsed["baseline"]["capacity_fill_rate_amount_weighted"], parsed["stress"]["capacity_fill_rate_amount_weighted"]) >= policy.minimum_capacity_fill_rate,
            "baseline_replay_passed": baseline.replay.get("passed") is True and not baseline.result.quality_errors,
            "stress_replay_passed": stress.replay.get("passed") is True and not stress.result.quality_errors,
        })
        reproduced_hashes = {}
        for scenario, artifact in (("baseline", baseline), ("stress", stress)):
            passed, result_hash = _reproduce_execution_protocol(artifact, source_root)
            reproduced_hashes[scenario] = result_hash
            checks[f"{scenario}_execution_protocol_reproduced"] = passed
        checks["execution_protocol_reproduced"] = all(
            checks[f"{scenario}_execution_protocol_reproduced"] for scenario in ("baseline", "stress")
        )
        current_rows = {
            row.id: row for row in db.scalars(select(ResearchEvidenceArtifact).where(
                ResearchEvidenceArtifact.id.in_(tuple(tracked_ids.values())),
            ).execution_options(populate_existing=True)).all()
        }
        if any(
            current_rows.get(artifact_id) is None
            or current_rows[artifact_id].status != "verified"
            or not verify_research_artifact(current_rows[artifact_id])
            or (current_rows[artifact_id].status, current_rows[artifact_id].evidence_hash) != registered_hashes[name]
            for name, artifact_id in tracked_ids.items()
        ):
            raise ValueError("portfolio_artifact_changed_during_resolution")
        resolved = {
            "release_id": release.id, "release_hash": release.release_hash,
            "target_status": "portfolio_passed", "pair_hash": pair["pair_hash"],
            "baseline_artifact_evidence_hash": registered_hashes["baseline"][1],
            "stress_artifact_evidence_hash": registered_hashes["stress"][1],
            "previous_evaluation_hash": previous_hash, "policy_hash": policy_hash,
            "metrics": metrics, "checks": checks, "execution_protocol_result_hashes": reproduced_hashes,
        }
        from server.services.manual_evidence import ResolvedReleaseEvidence

        return ResolvedReleaseEvidence("portfolio_passed", checks, dict(evidence_refs), _hash(resolved))
    except (OSError, StopIteration, InvalidOperation, KeyError, TypeError, ValueError) as exc:
        from server.services.manual_evidence import ManualEvidenceError

        if isinstance(exc, ManualEvidenceError):
            raise
        raise ManualEvidenceError(str(exc) or type(exc).__name__) from exc


__all__ = ["resolve_portfolio_passed"]
