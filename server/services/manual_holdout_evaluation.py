"""Frozen, leased economic execution for a manual holdout.

The registration half of this module intentionally reads only source metadata
and date columns.  Loading prices, actions, factors, or replay output is
allowed only after :func:`open_manual_holdout` has committed an access fact.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import uuid
from typing import Any, Callable

import pyarrow.parquet as pq
from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from quant_engine.factor.manual_daily_bundle import ManualDailyFactorBundleV2
from quant_engine.factor.expression import FactorExpressionSpec
from quant_engine.backtest.manual_portfolio_artifacts import controlled_path
from quant_engine.backtest.manual_portfolio_sources import _manifest_entry_file
from server.config import settings
from server.models.database import ensure_savepoint_transaction
from server.models.schema import (
    FactorCandidate,
    ManualHoldoutBinding,
    ManualHoldoutEvaluation,
    ResearchEvidenceArtifact,
    ResearchHoldoutAccess,
    ResearchHoldoutWindow,
    StrategyPromotionEvaluation,
    StrategyRelease,
    manual_now_str,
    uuid4_str,
)
from server.services.manual_holdout import (
    ManualHoldoutError,
    _artifact_snapshot,
    _date,
    _open_snapshot,
    _assert_open_snapshot,
    _object,
    _validate_binding,
    open_manual_holdout,
)
from server.services.research_holdout import _lock_registry


class ManualHoldoutEvaluationError(ManualHoldoutError):
    pass


LEASE_MINUTES = 30
EXIT_TAIL = 20
PROTOCOL = "manual-holdout-economic-evaluation-v1"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_text() -> str:
    return _now().isoformat()


def _lease_until() -> str:
    return (_now() + timedelta(minutes=LEASE_MINUTES)).isoformat()


def _json_file(path: Path, code: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ManualHoldoutEvaluationError(code) from exc
    if not isinstance(value, dict):
        raise ManualHoldoutEvaluationError(code)
    return value


def _root_paths() -> tuple[Path, Path]:
    try:
        output_root = Path(settings.result_dir).resolve(strict=True)
        source_root = output_root.parent.resolve(strict=True)
    except OSError as exc:
        raise ManualHoldoutEvaluationError("holdout_controlled_root_invalid") from exc
    return output_root, source_root


def _relative_source(path: str | Path, source_root: Path, *, allow_manifest_absolute: bool = False) -> Path:
    supplied = Path(path)
    if supplied.is_absolute() and not allow_manifest_absolute:
        raise ManualHoldoutEvaluationError("holdout_input_path_must_be_relative")
    if not supplied.is_absolute() and ".." in supplied.parts:
        raise ManualHoldoutEvaluationError("holdout_input_path_escape")
    try:
        resolved = controlled_path(supplied, source_root)
    except (OSError, ValueError) as exc:
        raise ManualHoldoutEvaluationError("holdout_input_path_outside_root") from exc
    return resolved


def _source_entry(path: Path, source_root: Path, *, role: str) -> dict[str, Any]:
    return {
        "role": role,
        "path": path.relative_to(source_root).as_posix(),
        "size": path.stat().st_size,
        "sha256": _file_hash(path),
    }


def _declared_entry(item: dict[str, Any], source_root: Path, *, role: str, path: Path) -> dict[str, Any]:
    """Build an identity entry from manifest metadata without hashing bytes."""
    return {
        "role": role,
        "path": path.relative_to(source_root).as_posix(),
        "size": int(item.get("size", path.stat().st_size)),
        "sha256": str(item.get("sha256")),
    }


def _date_column(path: Path, start: date, end: date, role: str, *, ordered: bool = False, unique: bool = False) -> list[date]:
    try:
        values = pq.read_table(path, columns=["date"]).column("date").to_pylist()
        days = [value.date() if hasattr(value, "date") else date.fromisoformat(str(value)) for value in values]
    except (OSError, TypeError, ValueError, KeyError, IndexError) as exc:
        raise ManualHoldoutEvaluationError(f"holdout_{role}_date_metadata_invalid") from exc
    if not days or any(day < start or day > end for day in days):
        raise ManualHoldoutEvaluationError(f"holdout_{role}_date_outside_manifest")
    if unique and len(days) != len(set(days)):
        raise ManualHoldoutEvaluationError(f"holdout_{role}_dates_duplicate")
    if ordered and any(left >= right for left, right in zip(days, days[1:])):
        raise ManualHoldoutEvaluationError(f"holdout_{role}_dates_not_sorted")
    return days


def _code_hashes() -> dict[str, str]:
    try:
        from server.services.manual_holdout_engine import get_holdout_engine_code_hashes
        values = dict(get_holdout_engine_code_hashes())
        root = Path(__file__).resolve().parents[2]
        local = {
            "evaluation_service": root / "server/services/manual_holdout_evaluation.py",
            "manual_holdout": root / "server/services/manual_holdout.py",
            "research_holdout": root / "server/services/research_holdout.py",
        }
        if any(not path.is_file() for path in local.values()):
            raise ManualHoldoutEvaluationError("holdout_code_identity_unavailable")
        values.update({name: _file_hash(path) for name, path in local.items()})
        return values
    except (ImportError, OSError, TypeError, ValueError) as exc:
        raise ManualHoldoutEvaluationError("holdout_code_identity_unavailable") from exc


def _artifact_manifest_path(artifact: ResearchEvidenceArtifact, source_root: Path) -> Path:
    payload = _object(artifact.manifest_json, "parent_artifact_manifest")
    files = payload.get("files")
    if not isinstance(files, list):
        raise ManualHoldoutEvaluationError("holdout_parent_manifest_invalid")
    item = next((item for item in files if isinstance(item, dict) and item.get("role") == "manifest"), None)
    if not isinstance(item, dict) or not isinstance(item.get("path"), str):
        raise ManualHoldoutEvaluationError("holdout_parent_manifest_file_missing")
    return _relative_source(item["path"], source_root, allow_manifest_absolute=True)


def _parent_metadata(
    db: Session,
    release: StrategyRelease,
    evaluation: StrategyPromotionEvaluation,
    source_root: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str]:
    refs = _object(evaluation.evidence_refs_json, "portfolio_evaluation_refs")
    if set(refs) != {"baseline_artifact_id", "stress_artifact_id"}:
        raise ManualHoldoutEvaluationError("holdout_parent_refs_invalid")
    manifests: dict[str, dict[str, Any]] = {}
    artifacts: dict[str, ResearchEvidenceArtifact] = {}
    for scenario, key in (("baseline", "baseline_artifact_id"), ("stress", "stress_artifact_id")):
        artifact = db.get(ResearchEvidenceArtifact, refs[key])
        if artifact is None or artifact.status != "verified":
            raise ManualHoldoutEvaluationError("holdout_parent_artifact_invalid")
        artifacts[scenario] = artifact
        path = _artifact_manifest_path(artifact, source_root)
        manifests[scenario] = _json_file(path, "holdout_parent_manifest_invalid")
        if manifests[scenario].get("scenario") != scenario:
            raise ManualHoldoutEvaluationError("holdout_parent_scenario_invalid")
    baseline, stress = manifests["baseline"], manifests["stress"]
    base_env, stress_env = baseline.get("run_envelope"), stress.get("run_envelope")
    if not isinstance(base_env, dict) or not isinstance(stress_env, dict):
        raise ManualHoldoutEvaluationError("holdout_parent_envelope_missing")
    bundle_payload = base_env.get("bundle")
    stress_bundle_payload = stress_env.get("bundle")
    try:
        bundle = ManualDailyFactorBundleV2.from_dict(bundle_payload)
        stress_bundle = ManualDailyFactorBundleV2.from_dict(stress_bundle_payload)
    except (TypeError, ValueError) as exc:
        raise ManualHoldoutEvaluationError("holdout_parent_bundle_invalid") from exc
    if bundle.cost_scenario != "baseline" or stress_bundle.cost_scenario != "stress":
        raise ManualHoldoutEvaluationError("holdout_parent_scenarios_invalid")
    if bundle.core_identity() != stress_bundle.core_identity() or bundle.strategy_core_hash != release.strategy_fingerprint:
        raise ManualHoldoutEvaluationError("holdout_parent_core_mismatch")
    if base_env.get("initial_capital") != stress_env.get("initial_capital"):
        raise ManualHoldoutEvaluationError("holdout_parent_capital_mismatch")
    input_manifest = baseline.get("input_manifest")
    if not isinstance(input_manifest, dict):
        raise ManualHoldoutEvaluationError("holdout_parent_input_manifest_missing")
    benchmark_id = input_manifest.get("benchmark_id")
    if not benchmark_id:
        raise ManualHoldoutEvaluationError("holdout_parent_benchmark_missing")
    parent = {
        "release_id": release.id,
        "release_hash": release.release_hash,
        "bundle_hash": release.bundle_hash,
        "strategy_core_hash": release.strategy_fingerprint,
        "portfolio_evaluation_id": evaluation.id,
        "portfolio_evaluation_hash": evaluation.evaluation_hash,
        "baseline_artifact_id": artifacts["baseline"].id,
        "baseline_artifact_hash": artifacts["baseline"].evidence_hash,
        "stress_artifact_id": artifacts["stress"].id,
        "stress_artifact_hash": artifacts["stress"].evidence_hash,
        "baseline_manifest_sha256": _file_hash(_artifact_manifest_path(artifacts["baseline"], source_root)),
        "stress_manifest_sha256": _file_hash(_artifact_manifest_path(artifacts["stress"], source_root)),
    }
    original = {
        "bundle": bundle.as_dict(),
        "strategy_core_hash": bundle.strategy_core_hash,
        "capital": str(base_env.get("initial_capital")),
        "benchmark_id": str(benchmark_id),
        "signal_start": base_env.get("start_date"),
        "signal_end": base_env.get("end_date"),
    }
    return parent, original, manifests, str(benchmark_id)


def _metadata_input(
    db: Session,
    binding: ManualHoldoutBinding,
    window: ResearchHoldoutWindow,
    release: StrategyRelease,
    portfolio_eval: StrategyPromotionEvaluation,
    dataset_manifest_path: str,
    benchmark_receipt_path: str,
    actor: str,
) -> tuple[dict[str, Any], Path, Path]:
    output_root, source_root = _root_paths()
    manifest_path = _relative_source(dataset_manifest_path, source_root)
    receipt_path = _relative_source(benchmark_receipt_path, source_root)
    manifest = _json_file(manifest_path, "holdout_dataset_manifest_invalid")
    receipt = _json_file(receipt_path, "holdout_benchmark_receipt_invalid")
    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) != {"daily", "actions", "securities", "calendar"}:
        raise ManualHoldoutEvaluationError("holdout_dataset_manifest_file_set_invalid")
    try:
        start, end = date.fromisoformat(str(manifest["start_date"])), date.fromisoformat(str(manifest["end_date"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ManualHoldoutEvaluationError("holdout_dataset_manifest_range_invalid") from exc
    if start > end or str(manifest.get("dataset_id")) != window.dataset_id or str(manifest.get("content_hash")) != window.data_content_hash:
        raise ManualHoldoutEvaluationError("holdout_dataset_identity_mismatch")
    identity = {
        "universe": manifest.get("universe"),
        "start_date": manifest.get("start_date"),
        "end_date": manifest.get("end_date"),
        "calendar_hash": (manifest.get("calendar") or {}).get("content_hash"),
        "files": {role: item.get("sha256") for role, item in files.items() if isinstance(item, dict)},
        "source_files": manifest.get("source_files"),
    }
    if _hash(identity) != manifest.get("content_hash"):
        raise ManualHoldoutEvaluationError("holdout_dataset_manifest_content_hash_mismatch")
    entries: dict[str, dict[str, Any]] = {}
    for role, item in files.items():
        if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not isinstance(item.get("sha256"), str):
            raise ManualHoldoutEvaluationError("holdout_dataset_source_metadata_invalid")
        try:
            path = _manifest_entry_file(item["path"], manifest_path.parent, source_root)
        except (OSError, TypeError, ValueError) as exc:
            raise ManualHoldoutEvaluationError("holdout_dataset_source_path_invalid") from exc
        if item.get("size") is not None and int(item["size"]) != path.stat().st_size:
            raise ManualHoldoutEvaluationError(f"holdout_{role}_size_changed")
        entries[role] = _declared_entry(item, source_root, role=role, path=path)
        # Date columns are structural metadata and are the only parquet data
        # permitted before the access fact is committed.  Actions and
        # securities remain entirely unopened until the engine runs.
        if role == "daily":
            _date_column(path, start, end, role)
        elif role == "calendar":
            _date_column(path, start, end, role, ordered=True, unique=True)
    try:
        calendar_path = _manifest_entry_file(files["calendar"]["path"], manifest_path.parent, source_root)
    except (OSError, TypeError, ValueError) as exc:
        raise ManualHoldoutEvaluationError("holdout_calendar_path_invalid") from exc
    calendar_days = _date_column(calendar_path, start, end, "calendar", ordered=True, unique=True)
    parent, original, _, expected_benchmark = _parent_metadata(db, release, portfolio_eval, source_root)
    exit_offset = ManualDailyFactorBundleV2.from_dict(original["bundle"]).exit_offset
    signal_start, signal_end = date.fromisoformat(window.start_date), date.fromisoformat(window.end_date)
    signal_days = [day for day in calendar_days if signal_start <= day <= signal_end]
    if not signal_days:
        raise ManualHoldoutEvaluationError("holdout_signal_window_calendar_empty")
    last_signal_index = calendar_days.index(signal_days[-1])
    if last_signal_index + exit_offset + EXIT_TAIL >= len(calendar_days):
        raise ManualHoldoutEvaluationError("holdout_dataset_missing_exit_tail")
    arguments = receipt.get("arguments")
    ticker = arguments.get("code") if isinstance(arguments, dict) else None
    if str(ticker) != expected_benchmark:
        raise ManualHoldoutEvaluationError("holdout_benchmark_identity_mismatch")
    try:
        benchmark_file = _relative_source(receipt_path.with_suffix(".parquet"), source_root, allow_manifest_absolute=True)
    except ManualHoldoutEvaluationError:
        raise ManualHoldoutEvaluationError("holdout_benchmark_file_missing")
    benchmark_days = _date_column(benchmark_file, start, end, "benchmark", unique=True)
    source_files = [entries[role] for role in sorted(entries)]
    source_files.extend((
        {"role": "dataset_manifest", "path": manifest_path.relative_to(source_root).as_posix(), "size": manifest_path.stat().st_size, "sha256": _file_hash(manifest_path)},
        {"role": "benchmark_receipt", "path": receipt_path.relative_to(source_root).as_posix(), "size": receipt_path.stat().st_size, "sha256": _file_hash(receipt_path)},
        {"role": "benchmark", "path": benchmark_file.relative_to(source_root).as_posix(), "size": benchmark_file.stat().st_size, "sha256": str(receipt.get("sha256"))},
    ))
    training_refs = _object(release.research_evidence, "research_evidence")
    training = db.get(ResearchEvidenceArtifact, training_refs.get("training_artifact_id"))
    if training is None:
        raise ManualHoldoutEvaluationError("holdout_training_artifact_missing")
    training_identity = _object(training.identity_json, "training_identity")
    candidate = db.get(FactorCandidate, training_identity.get("candidate_id"))
    if candidate is None:
        raise ManualHoldoutEvaluationError("holdout_factor_candidate_missing")
    expression = _object(candidate.expression_spec, "factor_expression_spec")
    if candidate.expression_hash != training_identity.get("expression_hash") or candidate.role != "rank" or candidate.direction not in (-1, 1):
        raise ManualHoldoutEvaluationError("holdout_factor_identity_invalid")
    if candidate.direction != int(training_identity.get("direction", candidate.direction)):
        raise ManualHoldoutEvaluationError("holdout_factor_direction_mismatch")
    try:
        expression_spec = FactorExpressionSpec.from_dict(expression)
    except (TypeError, ValueError) as exc:
        raise ManualHoldoutEvaluationError("holdout_factor_expression_invalid") from exc
    if expression_spec.expression_hash != candidate.expression_hash or expression_spec.direction != candidate.direction or expression_spec.role != candidate.role:
        raise ManualHoldoutEvaluationError("holdout_factor_expression_mismatch")
    validation_id = training_refs.get("validation_artifact_id")
    validation = db.get(ResearchEvidenceArtifact, validation_id)
    if validation is None:
        raise ManualHoldoutEvaluationError("holdout_validation_artifact_missing")
    input_payload = {
        "protocol_version": PROTOCOL,
        "binding": {"id": binding.id, "binding_hash": binding.binding_hash, "window_id": window.id, "release_id": binding.release_id, "protocol_hash": binding.protocol_hash, "release_hash": binding.release_hash, "portfolio_evaluation_id": binding.portfolio_evaluation_id, "portfolio_evaluation_hash": binding.portfolio_evaluation_hash},
        "actor": actor,
        "parent": {
            **parent,
            "training_artifact_id": training.id,
            "training_artifact_hash": training.evidence_hash,
            "validation_artifact_id": validation.id,
            "validation_artifact_hash": validation.evidence_hash,
        },
        "original": original,
        "factor_expression": {"candidate_id": candidate.id, "expression_hash": candidate.expression_hash, "spec": expression, "role": candidate.role, "direction": candidate.direction},
        "dataset": {"dataset_id": window.dataset_id, "data_content_hash": window.data_content_hash, "manifest_path": manifest_path.relative_to(source_root).as_posix(), "manifest_size": manifest_path.stat().st_size, "manifest_sha256": _file_hash(manifest_path), "start_date": start.isoformat(), "end_date": end.isoformat(), "files": entries, "calendar_days": len(calendar_days), "read_scope_start": start.isoformat(), "read_scope_end": end.isoformat()},
        "benchmark": {"ticker": str(ticker), "receipt_path": receipt_path.relative_to(source_root).as_posix(), "receipt_size": receipt_path.stat().st_size, "receipt_sha256": _file_hash(receipt_path), "parquet": {"role": "benchmark", "path": benchmark_file.relative_to(source_root).as_posix(), "size": benchmark_file.stat().st_size, "sha256": str(receipt.get("sha256"))}, "date_start": min(benchmark_days).isoformat(), "date_end": max(benchmark_days).isoformat()},
        "source_files": source_files,
        "signal_window": {"requested_start_date": window.start_date, "requested_end_date": window.end_date, "start_date": signal_days[0].isoformat(), "end_date": signal_days[-1].isoformat(), "exit_tail_trading_days": EXIT_TAIL},
        "capital": original["capital"],
        "technical_policy": {"technical_exit_tail_v1": EXIT_TAIL, "source_dataset_full_read": True},
        "code_hashes": _code_hashes(),
    }
    return input_payload, manifest_path, receipt_path


def freeze_holdout_evaluation(
    db: Session,
    *,
    binding_id: str,
    dataset_manifest_path: str,
    benchmark_receipt_path: str,
    actor: str,
    idempotency_key: str,
) -> ManualHoldoutEvaluation:
    actor, request_key = str(actor).strip(), str(idempotency_key).strip()
    if not actor:
        raise ManualHoldoutEvaluationError("holdout_evaluation_actor_required")
    if not request_key:
        raise ManualHoldoutEvaluationError("holdout_evaluation_request_key_required")
    binding = db.get(ManualHoldoutBinding, binding_id)
    if binding is None:
        raise ManualHoldoutEvaluationError("holdout_binding_not_found")
    existing_key = db.scalars(select(ManualHoldoutEvaluation).where(ManualHoldoutEvaluation.request_key == request_key)).first()
    if existing_key is not None and existing_key.binding_id != binding_id:
        raise ManualHoldoutEvaluationError("holdout_evaluation_request_key_conflict")
    try:
        window, release, portfolio_eval, _ = _validate_binding(db, binding)
        if portfolio_eval is None:
            raise ManualHoldoutEvaluationError("holdout_parent_missing")
        if window.status in {"invalidated", "completed"}:
            raise ManualHoldoutEvaluationError(f"holdout_{window.status}")
        payload, _, _ = _metadata_input(db, binding, window, release, portfolio_eval, dataset_manifest_path, benchmark_receipt_path, actor)
        input_hash = _hash(payload)
        if existing_key is not None:
            old = _runtime_input(existing_key)
            if old.get("dataset", {}).get("manifest_path") != payload.get("dataset", {}).get("manifest_path") or old.get("benchmark", {}).get("receipt_path") != payload.get("benchmark", {}).get("receipt_path"):
                raise ManualHoldoutEvaluationError("holdout_evaluation_request_key_conflict")
            if existing_key.input_hash != input_hash:
                raise ManualHoldoutEvaluationError("holdout_evaluation_identity_conflict")
            return existing_key
        existing_binding = db.scalars(select(ManualHoldoutEvaluation).where(ManualHoldoutEvaluation.binding_id == binding_id)).first()
        if existing_binding is not None:
            if existing_binding.input_hash != input_hash:
                raise ManualHoldoutEvaluationError("holdout_evaluation_identity_conflict")
            return existing_binding
        scope_start = date.fromisoformat(payload["dataset"]["read_scope_start"])
        scope_end = date.fromisoformat(payload["dataset"]["read_scope_end"])
        ensure_savepoint_transaction(db)
        with db.begin_nested():
            _lock_registry(db)
            for other in db.scalars(select(ResearchHoldoutWindow).where(ResearchHoldoutWindow.id != window.id)).all():
                if other.status in {"sealed", "opened"} and _date(other.start_date) <= scope_end and _date(other.end_date) >= scope_start:
                    raise ManualHoldoutEvaluationError("holdout_evaluation_scope_conflict")
            for other in db.scalars(select(ManualHoldoutEvaluation).where(ManualHoldoutEvaluation.id != "")).all():
                if not other.read_scope_start or not other.read_scope_end:
                    continue
                other_window = db.get(ResearchHoldoutWindow, db.get(ManualHoldoutBinding, other.binding_id).window_id) if db.get(ManualHoldoutBinding, other.binding_id) else None
                exclusive = other.status in {"pending", "running"} or (other_window is not None and other_window.status in {"sealed", "opened"})
                if exclusive and _date(other.read_scope_start) <= scope_end and _date(other.read_scope_end) >= scope_start:
                    raise ManualHoldoutEvaluationError("holdout_evaluation_scope_conflict")
            row = ManualHoldoutEvaluation(
                id=uuid4_str(), binding_id=binding_id, input_json=_canonical(payload), input_hash=input_hash,
                request_key=request_key, created_by=actor, status="pending",
                read_scope_start=scope_start.isoformat(), read_scope_end=scope_end.isoformat(),
            )
            db.add(row)
            db.flush()
        db.commit()
        db.refresh(row)
        return row
    except (ManualHoldoutError, ManualHoldoutEvaluationError):
        db.rollback()
        raise
    except IntegrityError as exc:
        db.rollback()
        raise ManualHoldoutEvaluationError("holdout_evaluation_conflict") from exc
    except Exception:
        db.rollback()
        raise


def _runtime_input(row: ManualHoldoutEvaluation) -> dict[str, Any]:
    try:
        payload = json.loads(row.input_json)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ManualHoldoutEvaluationError("holdout_evaluation_input_invalid") from exc
    if not isinstance(payload, dict) or _hash(payload) != row.input_hash or payload.get("protocol_version") != PROTOCOL:
        raise ManualHoldoutEvaluationError("holdout_evaluation_input_hash_mismatch")
    return payload


def _verify_runtime_sources(payload: dict[str, Any]) -> tuple[Path, Path, Path, Path]:
    _, source_root = _root_paths()
    dataset = payload.get("dataset", {})
    benchmark = payload.get("benchmark", {})
    manifest = _relative_source(dataset.get("manifest_path"), source_root)
    receipt = _relative_source(benchmark.get("receipt_path"), source_root)
    benchmark_file = _relative_source(receipt.with_suffix(".parquet"), source_root, allow_manifest_absolute=True)
    if _file_hash(manifest) != dataset.get("manifest_sha256") or manifest.stat().st_size != dataset.get("manifest_size"):
        raise ManualHoldoutEvaluationError("holdout_dataset_manifest_changed")
    if _file_hash(receipt) != benchmark.get("receipt_sha256") or receipt.stat().st_size != benchmark.get("receipt_size"):
        raise ManualHoldoutEvaluationError("holdout_benchmark_receipt_changed")
    current = _code_hashes()
    if current != payload.get("code_hashes"):
        raise ManualHoldoutEvaluationError("holdout_evaluation_code_changed")
    return manifest, receipt, benchmark_file, source_root


def _result_manifest_payload(
    top_path: Path, verified: dict[str, Any], payload: dict[str, Any], evaluation_id: str,
) -> dict[str, Any]:
    """Persist only external file anchors, never audited Python objects."""
    top = verified.get("manifest")
    if not isinstance(top, dict) or not isinstance(top.get("scenarios"), dict):
        raise ManualHoldoutEvaluationError("holdout_engine_manifest_shape_invalid")
    entries: dict[str, Any] = {}
    top_relative = top_path.relative_to(Path(settings.result_dir).resolve(strict=True)).as_posix()
    entries["top_level"] = {"role": "top_level_manifest", "path": top_relative, "size": top_path.stat().st_size, "sha256": _file_hash(top_path)}
    scenario_payload: dict[str, Any] = {}
    expected_names = {
        "signals.parquet", "order_intents.parquet", "order_attempts.parquet", "trades.parquet",
        "corporate_actions.parquet", "positions.parquet", "daily_portfolio.parquet", "benchmark.parquet",
        "audit.json", "replay.json", "summary.json",
    }
    for scenario in ("baseline", "stress"):
        item = top["scenarios"].get(scenario)
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise ManualHoldoutEvaluationError("holdout_scenario_manifest_missing")
        scenario_dir = (top_path.parent / item["path"]).resolve(strict=True)
        if top_path.parent not in scenario_dir.parents:
            raise ManualHoldoutEvaluationError("holdout_scenario_path_escape")
        scenario_manifest = _json_file(scenario_dir / "manifest.json", "holdout_scenario_manifest_invalid")
        files = scenario_manifest.get("files")
        if not isinstance(files, list) or len(files) != 11:
            raise ManualHoldoutEvaluationError("holdout_scenario_file_set_invalid")
        anchors = []
        seen: set[str] = set()
        for file_item in files:
            if not isinstance(file_item, dict) or not isinstance(file_item.get("path"), str) or file_item.get("path") in seen:
                raise ManualHoldoutEvaluationError("holdout_scenario_file_set_invalid")
            name = str(file_item["path"])
            if name not in expected_names:
                raise ManualHoldoutEvaluationError("holdout_scenario_file_set_invalid")
            seen.add(name)
            file_path = (scenario_dir / name).resolve(strict=True)
            if file_path.parent != scenario_dir or not file_path.is_file():
                raise ManualHoldoutEvaluationError("holdout_scenario_file_path_invalid")
            anchors.append({"role": file_item.get("role"), "path": file_path.relative_to(Path(settings.result_dir).resolve(strict=True)).as_posix(), "size": file_path.stat().st_size, "sha256": _file_hash(file_path)})
        manifest_path = scenario_dir / "manifest.json"
        anchors.append({"role": "manifest", "path": manifest_path.relative_to(Path(settings.result_dir).resolve(strict=True)).as_posix(), "size": manifest_path.stat().st_size, "sha256": _file_hash(manifest_path)})
        scenario_payload[scenario] = {"manifest": anchors[-1], "files": anchors}
    return {
        "protocol_version": PROTOCOL,
        "evaluation_id": evaluation_id,
        "pair_hash": verified.get("pair_hash"),
        "input_hash": _hash(payload),
        "original_strategy_core_hash": payload["original"]["strategy_core_hash"],
        "derived_strategy_core_hash": verified.get("derived_strategy_core_hash"),
        "evaluation_core_hash": verified.get("evaluation_core_hash"),
        "metrics": verified.get("metrics", {}),
        "top_level": entries["top_level"],
        "scenarios": scenario_payload,
    }


def _heartbeat(db: Session, evaluation_id: str, token: str) -> ManualHoldoutEvaluation:
    changed = db.execute(update(ManualHoldoutEvaluation).where(
        ManualHoldoutEvaluation.id == evaluation_id,
        ManualHoldoutEvaluation.status == "running",
        ManualHoldoutEvaluation.lease_token == token,
        ManualHoldoutEvaluation.lease_until > _now_text(),
    ).values(lease_until=_lease_until()))
    if changed.rowcount != 1:
        db.rollback()
        raise ManualHoldoutEvaluationError("holdout_evaluation_lease_lost")
    db.commit()
    row = db.get(ManualHoldoutEvaluation, evaluation_id)
    if row is None:
        raise ManualHoldoutEvaluationError("holdout_evaluation_not_found")
    return row


def _assert_access(db: Session, row: ManualHoldoutEvaluation, binding: ManualHoldoutBinding, access_id: str, attempt: int) -> ResearchHoldoutAccess:
    access = db.get(ResearchHoldoutAccess, access_id)
    if access is None or access.window_id != binding.window_id or access.binding_hash != binding.binding_hash or not access.result_exposed:
        raise ManualHoldoutEvaluationError("holdout_evaluation_access_invalid")
    if access.purpose != f"holdout-evaluation:{row.id}:attempt:{attempt}":
        raise ManualHoldoutEvaluationError("holdout_evaluation_access_purpose_invalid")
    expected = _hash({
        "binding_hash": binding.binding_hash, "window_id": binding.window_id,
        "accessed_by": access.accessed_by, "purpose": access.purpose,
        "accessed_at": access.accessed_at, "result_exposed": True,
    })
    if access.payload_hash != expected:
        raise ManualHoldoutEvaluationError("holdout_evaluation_access_hash_invalid")
    return access


def _assert_parent_snapshot(
    db: Session, payload: dict[str, Any], binding: ManualHoldoutBinding,
    window: ResearchHoldoutWindow, release: StrategyRelease,
    portfolio_eval: StrategyPromotionEvaluation,
) -> None:
    parent = payload.get("parent", {})
    if (
        binding.binding_hash != payload.get("binding", {}).get("binding_hash")
        or binding.protocol_hash != payload.get("binding", {}).get("protocol_hash")
        or binding.portfolio_evaluation_id != payload.get("binding", {}).get("portfolio_evaluation_id")
        or binding.release_id != release.id
        or binding.release_hash != parent.get("release_hash")
        or binding.portfolio_evaluation_hash != parent.get("portfolio_evaluation_hash")
        or release.release_hash != parent.get("release_hash")
        or release.bundle_hash != parent.get("bundle_hash")
        or release.strategy_fingerprint != parent.get("strategy_core_hash")
        or release.status != "portfolio_passed"
        or portfolio_eval.id != parent.get("portfolio_evaluation_id")
        or portfolio_eval.evaluation_hash != parent.get("portfolio_evaluation_hash")
        or window.dataset_id != payload.get("dataset", {}).get("dataset_id")
        or window.data_content_hash != payload.get("dataset", {}).get("data_content_hash")
        or window.start_date != payload.get("signal_window", {}).get("requested_start_date")
        or window.end_date != payload.get("signal_window", {}).get("requested_end_date")
        or window.status != "opened"
    ):
        raise ManualHoldoutEvaluationError("holdout_evaluation_parent_changed")
    for key, kind in (("baseline", "manual_portfolio_baseline"), ("stress", "manual_portfolio_stress")):
        artifact_id = parent.get(f"{key}_artifact_id")
        artifact_hash = parent.get(f"{key}_artifact_hash")
        artifact = db.get(ResearchEvidenceArtifact, artifact_id)
        if artifact is not None:
            db.refresh(artifact)
        if artifact is None or artifact.kind != kind or artifact.status != "verified" or artifact.evidence_hash != artifact_hash:
            raise ManualHoldoutEvaluationError("holdout_evaluation_parent_artifact_changed")
    training_id = parent.get("training_artifact_id")
    validation_id = parent.get("validation_artifact_id")
    for artifact_id, kind, key in ((training_id, "factor_training", "training_artifact_hash"), (validation_id, "factor_validation", "validation_artifact_hash")):
        artifact = db.get(ResearchEvidenceArtifact, artifact_id)
        if artifact is not None:
            db.refresh(artifact)
        if artifact is None or artifact.kind != kind or artifact.status != "verified" or artifact.evidence_hash != parent.get(key):
            raise ManualHoldoutEvaluationError("holdout_evaluation_parent_artifact_changed")


def _mark_failed(db: Session, evaluation_id: str, token: str, error: Exception) -> None:
    try:
        db.rollback()
        db.execute(update(ManualHoldoutEvaluation).where(
            ManualHoldoutEvaluation.id == evaluation_id,
            ManualHoldoutEvaluation.status == "running",
            ManualHoldoutEvaluation.lease_token == token,
            ManualHoldoutEvaluation.lease_until > _now_text(),
        ).values(status="failed", last_error=str(error)[:4000], lease_token=None, lease_until=None))
        db.commit()
    except Exception:
        db.rollback()


def run_holdout_evaluation(db: Session, evaluation_id: str, *, actor: str) -> ManualHoldoutEvaluation:
    actor = str(actor).strip()
    row = db.get(ManualHoldoutEvaluation, evaluation_id)
    if row is None:
        raise ManualHoldoutEvaluationError("holdout_evaluation_not_found")
    if row.status == "completed":
        verify_completed_holdout(db, row.result_artifact_id or "")
        return row
    now = _now_text()
    token = uuid.uuid4().hex
    changed = db.execute(update(ManualHoldoutEvaluation).where(
        ManualHoldoutEvaluation.id == evaluation_id,
        or_(
            ManualHoldoutEvaluation.status.in_(("pending", "failed")),
            and_(ManualHoldoutEvaluation.status == "running", or_(ManualHoldoutEvaluation.lease_until.is_(None), ManualHoldoutEvaluation.lease_until <= now)),
        ),
    ).values(status="running", attempt_count=ManualHoldoutEvaluation.attempt_count + 1, lease_token=token, lease_until=_lease_until(), last_error=None))
    if changed.rowcount != 1:
        db.rollback()
        raise ManualHoldoutEvaluationError("holdout_evaluation_busy")
    db.commit()
    row = db.get(ManualHoldoutEvaluation, evaluation_id)
    attempt = row.attempt_count
    try:
        payload = _runtime_input(row)
        _, _, _, source_root = _verify_runtime_sources(payload)
        binding = db.get(ManualHoldoutBinding, row.binding_id)
        if binding is None:
            raise ManualHoldoutEvaluationError("holdout_binding_not_found")
        # The access write commits before any engine/source call.
        access = open_manual_holdout(
            db, binding.id, actor=actor or row.created_by,
            purpose=f"holdout-evaluation:{row.id}:attempt:{attempt}",
        )
        changed = db.execute(update(ManualHoldoutEvaluation).where(
            ManualHoldoutEvaluation.id == row.id, ManualHoldoutEvaluation.status == "running", ManualHoldoutEvaluation.lease_token == token, ManualHoldoutEvaluation.lease_until > _now_text(),
        ).values(access_id=access.id))
        if changed.rowcount != 1:
            raise ManualHoldoutEvaluationError("holdout_evaluation_lease_lost")
        db.commit()
        _assert_access(db, row, binding, access.id, attempt)
        _heartbeat(db, row.id, token)
        snapshot_window = db.get(ResearchHoldoutWindow, binding.window_id)
        snapshot_release = db.get(StrategyRelease, binding.release_id)
        snapshot_eval = db.get(StrategyPromotionEvaluation, binding.portfolio_evaluation_id)
        if snapshot_window is None or snapshot_release is None or snapshot_eval is None:
            raise ManualHoldoutEvaluationError("holdout_evaluation_parent_changed")
        snapshot_artifacts = _artifact_snapshot(db, snapshot_release, snapshot_eval)
        parent_snapshot = _open_snapshot(binding, snapshot_window, snapshot_release, snapshot_eval, snapshot_artifacts)
        output_root, _ = _root_paths()
        from server.services.manual_holdout_engine import run_holdout_engine, verify_holdout_engine_manifest
        output_dir = output_root / "holdout-evaluations" / row.id / f"attempt-{attempt}-{token}"
        manifest_path = run_holdout_engine(
            payload, source_root=source_root, output_dir=output_dir, evaluation_id=row.id,
            attempt=attempt, token=token, access_id=access.id,
            heartbeat=lambda *_: _heartbeat(db, row.id, token),
        )
        _heartbeat(db, row.id, token)
        verified = verify_holdout_engine_manifest(
            Path(manifest_path), input_json=payload, source_root=source_root,
            allowed_root=output_root, expected_access_id=access.id,
        )
        if not isinstance(verified, dict):
            raise ManualHoldoutEvaluationError("holdout_engine_verification_invalid")
        for required in ("pair_hash", "derived_strategy_core_hash", "evaluation_core_hash", "metrics"):
            if required not in verified:
                raise ManualHoldoutEvaluationError(f"holdout_engine_verification_missing:{required}")
        _heartbeat(db, row.id, token)
        db.refresh(row)
        binding = db.get(ManualHoldoutBinding, row.binding_id)
        window = db.get(ResearchHoldoutWindow, binding.window_id if binding else "")
        release = db.get(StrategyRelease, binding.release_id if binding else "")
        portfolio_eval = db.get(StrategyPromotionEvaluation, binding.portfolio_evaluation_id if binding else "")
        if not binding or not window or not release or not portfolio_eval or window.status != "opened":
            raise ManualHoldoutEvaluationError("holdout_evaluation_parent_changed")
        _validate_binding(db, binding)
        manifest_path = Path(manifest_path).resolve(strict=True)
        result_root, _ = _root_paths()
        try:
            manifest_rel = manifest_path.relative_to(result_root).as_posix()
        except ValueError as exc:
            raise ManualHoldoutEvaluationError("holdout_result_outside_root") from exc
        result_manifest_sha = str(verified.get("manifest_file_sha256") or _file_hash(manifest_path))
        identity = {
            "evaluation_id": row.id, "attempt": attempt, "binding_id": binding.id,
            "binding_hash": binding.binding_hash, "input_hash": row.input_hash,
            "parent_release_hash": release.release_hash, "parent_core_hash": release.strategy_fingerprint,
            "parent_portfolio_evaluation_id": portfolio_eval.id,
            "parent_portfolio_evaluation_hash": portfolio_eval.evaluation_hash,
            "access_id": access.id, "pair_hash": verified["pair_hash"],
            "original_strategy_core_hash": payload["original"]["strategy_core_hash"],
            "derived_strategy_core_hash": verified["derived_strategy_core_hash"],
            "evaluation_core_hash": verified["evaluation_core_hash"],
        }
        artifact_manifest = _result_manifest_payload(manifest_path, verified, payload, row.id)
        identity_hash, manifest_hash = _hash(identity), _hash(artifact_manifest)
        evidence_hash = _hash({"identity_hash": identity_hash, "manifest_hash": manifest_hash})
        ensure_savepoint_transaction(db)
        with db.begin_nested():
            db.refresh(binding)
            db.refresh(window)
            db.refresh(release)
            db.refresh(portfolio_eval)
            _assert_access(db, row, binding, access.id, attempt)
            current_artifacts = _artifact_snapshot(db, release, portfolio_eval)
            _assert_open_snapshot(parent_snapshot, binding, window, release, portfolio_eval, current_artifacts)
            _assert_parent_snapshot(db, payload, binding, window, release, portfolio_eval)
            changed = db.execute(update(ManualHoldoutEvaluation).where(
                ManualHoldoutEvaluation.id == row.id, ManualHoldoutEvaluation.status == "running", ManualHoldoutEvaluation.lease_token == token, ManualHoldoutEvaluation.lease_until > _now_text(),
            ).values(status="completed", result_manifest_path=manifest_rel, result_manifest_sha256=result_manifest_sha,
                     result_artifact_id=uuid4_str(), completed_at=_now_text(), lease_token=None, lease_until=None))
            if changed.rowcount != 1:
                raise ManualHoldoutEvaluationError("holdout_evaluation_lease_lost")
            current = db.get(ManualHoldoutEvaluation, row.id)
            artifact = ResearchEvidenceArtifact(
                id=current.result_artifact_id, kind="holdout_result", producer_protocol=PROTOCOL,
                producer_entity_type="manual_holdout_evaluation", producer_entity_id=row.id,
                producer_attempt=attempt, status="verified", identity_json=_canonical(identity), identity_hash=identity_hash,
                manifest_json=_canonical(artifact_manifest), manifest_hash=manifest_hash, evidence_hash=evidence_hash,
            )
            db.add(artifact)
            db.flush()
            changed_window = db.execute(update(ResearchHoldoutWindow).where(
                ResearchHoldoutWindow.id == window.id, ResearchHoldoutWindow.status == "opened",
            ).values(status="completed", completed_at=_now_text()))
            if changed_window.rowcount != 1:
                raise ManualHoldoutEvaluationError("holdout_window_status_changed")
        db.commit()
        db.refresh(current)
        return current
    except Exception as exc:
        _mark_failed(db, evaluation_id, token, exc)
        raise


def verify_completed_holdout(db: Session, artifact_id: str) -> dict[str, Any]:
    artifact = db.get(ResearchEvidenceArtifact, artifact_id)
    if artifact is None or artifact.kind != "holdout_result" or artifact.status != "verified":
        raise ManualHoldoutEvaluationError("holdout_result_artifact_invalid")
    evaluation = db.get(ManualHoldoutEvaluation, artifact.producer_entity_id)
    if evaluation is None or evaluation.status != "completed" or evaluation.result_artifact_id != artifact.id:
        raise ManualHoldoutEvaluationError("holdout_result_evaluation_invalid")
    payload = _runtime_input(evaluation)
    _verify_runtime_sources(payload)
    result_root, _ = _root_paths()
    try:
        manifest_path = controlled_path(evaluation.result_manifest_path or "", result_root)
    except (OSError, ValueError) as exc:
        raise ManualHoldoutEvaluationError("holdout_result_manifest_missing")
    if evaluation.result_manifest_sha256 != _file_hash(manifest_path):
        raise ManualHoldoutEvaluationError("holdout_result_manifest_changed")
    identity = _object(artifact.identity_json, "holdout_result_identity")
    manifest = _object(artifact.manifest_json, "holdout_result_manifest")
    if artifact.identity_hash != _hash(identity) or artifact.manifest_hash != _hash(manifest) or artifact.evidence_hash != _hash({"identity_hash": artifact.identity_hash, "manifest_hash": artifact.manifest_hash}):
        raise ManualHoldoutEvaluationError("holdout_result_artifact_hash_mismatch")
    binding = db.get(ManualHoldoutBinding, evaluation.binding_id)
    if binding is None:
        raise ManualHoldoutEvaluationError("holdout_binding_not_found")
    window, release, parent, _ = _validate_binding(db, binding, verify_parent=False)
    from server.services.manual_holdout import _verify_portfolio_parent
    parent = _verify_portfolio_parent(db, release, require_portfolio_status=False)
    if parent.id != binding.portfolio_evaluation_id or parent.evaluation_hash != binding.portfolio_evaluation_hash:
        raise ManualHoldoutEvaluationError("holdout_parent_evidence_mismatch")
    if window.status != "completed" or identity.get("access_id") != evaluation.access_id or identity.get("input_hash") != evaluation.input_hash:
        raise ManualHoldoutEvaluationError("holdout_result_identity_mismatch")
    if (
        artifact.producer_protocol != PROTOCOL
        or artifact.producer_entity_type != "manual_holdout_evaluation"
        or artifact.producer_entity_id != evaluation.id
        or artifact.producer_attempt != evaluation.attempt_count
    ):
        raise ManualHoldoutEvaluationError("holdout_result_producer_identity_mismatch")
    access = _assert_access(db, evaluation, binding, evaluation.access_id or "", evaluation.attempt_count)
    _, source_root = _root_paths()
    from server.services.manual_holdout_engine import verify_holdout_engine_manifest
    readback = verify_holdout_engine_manifest(
        manifest_path, input_json=payload, source_root=source_root,
        allowed_root=Path(settings.result_dir).resolve(strict=True), expected_access_id=access.id,
    )
    top = readback.get("manifest")
    if not isinstance(top, dict) or top.get("evaluation_id") != evaluation.id or top.get("attempt") != evaluation.attempt_count:
        raise ManualHoldoutEvaluationError("holdout_result_manifest_identity_mismatch")
    if not isinstance(readback, dict) or not isinstance(readback.get("metrics"), dict) or not isinstance(readback.get("independent_replays"), dict) or "pair_hash" not in readback or "quality_errors" not in readback:
        raise ManualHoldoutEvaluationError("holdout_result_readback_contract_invalid")
    if set(readback["metrics"]) != {"baseline", "stress"} or set(readback["independent_replays"]) != {"baseline", "stress"}:
        raise ManualHoldoutEvaluationError("holdout_result_scenario_contract_invalid")
    rebuilt_manifest = _result_manifest_payload(manifest_path, readback, payload, evaluation.id)
    if rebuilt_manifest != manifest:
        raise ManualHoldoutEvaluationError("holdout_result_manifest_anchor_mismatch")
    expected_identity = {
        "evaluation_id": evaluation.id, "attempt": evaluation.attempt_count,
        "binding_id": binding.id, "binding_hash": binding.binding_hash,
        "input_hash": evaluation.input_hash, "parent_release_hash": release.release_hash,
        "parent_core_hash": release.strategy_fingerprint,
        "parent_portfolio_evaluation_id": parent.id,
        "parent_portfolio_evaluation_hash": parent.evaluation_hash,
        "access_id": access.id, "pair_hash": readback["pair_hash"],
        "original_strategy_core_hash": payload["original"]["strategy_core_hash"],
        "derived_strategy_core_hash": readback.get("derived_strategy_core_hash"),
        "evaluation_core_hash": readback.get("evaluation_core_hash"),
    }
    if identity != expected_identity:
        raise ManualHoldoutEvaluationError("holdout_result_identity_mismatch")
    access_rows = db.scalars(select(ResearchHoldoutAccess).where(
        ResearchHoldoutAccess.window_id == binding.window_id,
    ).order_by(ResearchHoldoutAccess.accessed_at.asc(), ResearchHoldoutAccess.id.asc())).all()
    access_payloads = []
    for item in access_rows:
        expected_payload_hash = _hash({
            "binding_hash": binding.binding_hash, "window_id": binding.window_id,
            "accessed_by": item.accessed_by, "purpose": item.purpose,
            "accessed_at": item.accessed_at, "result_exposed": bool(item.result_exposed),
        })
        if item.binding_hash != binding.binding_hash or item.payload_hash != expected_payload_hash or not item.result_exposed:
            raise ManualHoldoutEvaluationError("holdout_result_access_hash_invalid")
        access_payloads.append({
            "id": item.id, "window_id": item.window_id, "accessed_at": item.accessed_at,
            "accessed_by": item.accessed_by, "purpose": item.purpose,
            "result_exposed": item.result_exposed, "binding_hash": item.binding_hash,
            "payload_hash": item.payload_hash,
        })
    return {
        "artifact_id": artifact.id,
        "evaluation_id": evaluation.id,
        "binding_id": binding.id,
        "metrics": readback.get("metrics", {}),
        "independent_replays": readback["independent_replays"],
        "quality_errors": readback["quality_errors"],
        "accesses": access_payloads,
        "pair_hash": readback["pair_hash"],
        "original_strategy_core_hash": identity.get("original_strategy_core_hash"),
        "derived_strategy_core_hash": identity.get("derived_strategy_core_hash"),
        "evaluation_core_hash": identity.get("evaluation_core_hash"),
        "parent_portfolio_evaluation_id": parent.id if parent else binding.portfolio_evaluation_id,
        "parent_portfolio_evaluation_hash": parent.evaluation_hash if parent else binding.portfolio_evaluation_hash,
        "verified": True,
    }


__all__ = [
    "ManualHoldoutEvaluationError", "freeze_holdout_evaluation", "run_holdout_evaluation",
    "verify_completed_holdout",
]
