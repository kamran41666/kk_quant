"""Register and re-verify persisted manual portfolio evidence pairs.

This service is deliberately kept separate from the read-only portfolio
artifact reader.  The reader supplies the independent replay; this module
binds that replay to the already verified factor evidence in the database.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_EVEN
import json
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from quant_engine.backtest.manual_portfolio_artifacts import (
    audit_portfolio_pair,
    controlled_path,
)
from quant_engine.backtest.manual_portfolio_evidence import (
    _file_hash,
    _hash,
    verify_portfolio_evidence_directory,
)
from quant_engine.factor.manual_daily_label import MANUAL_DAILY_LABEL_V1
from quant_engine.trading.manual_protocol import stable_hash
from server.config import settings
from server.models.schema import ResearchEvidenceArtifact, uuid4_str
from server.services.manual_evidence import (
    ManualEvidenceError,
    _MANUAL_LABEL,
    _reverify_factor_artifact,
    verify_research_artifact,
)


PRODUCER_PROTOCOL = "manual-daily-portfolio-evidence-v2"
ENTITY_TYPE = "manual_portfolio_pair"
_SCENARIOS = ("baseline", "stress")
_PORTFOLIO_KINDS = {
    "baseline": "manual_portfolio_baseline",
    "stress": "manual_portfolio_stress",
}
_PORTFOLIO_FILES = (
    "signals.parquet", "order_intents.parquet", "order_attempts.parquet",
    "trades.parquet", "corporate_actions.parquet", "positions.parquet",
    "daily_portfolio.parquet", "benchmark.parquet", "audit.json",
    "replay.json", "summary.json", "manifest.json",
)
_SCORE_QUANTUM = Decimal("0.000000000000000001")


def _json(value: str, name: str) -> Any:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ManualEvidenceError(f"manual_portfolio_{name}_json_invalid") from exc
    if not isinstance(parsed, dict):
        raise ManualEvidenceError(f"manual_portfolio_{name}_json_invalid")
    return parsed


def _root(value: str | Path | None, default: Path) -> Path:
    candidate = Path(value) if value is not None else default
    try:
        return candidate.resolve(strict=True)
    except OSError as exc:
        raise ManualEvidenceError("manual_portfolio_controlled_root_invalid") from exc


def _safe_path(path: str | Path, root: Path, code: str) -> Path:
    try:
        return controlled_path(path, root)
    except (OSError, TypeError, ValueError) as exc:
        raise ManualEvidenceError(code) from exc


def _factor_scope(artifact: ResearchEvidenceArtifact, source_root: Path) -> tuple[dict[str, Any], Path]:
    manifest = _json(artifact.manifest_json, "factor_manifest")
    if not isinstance(manifest, dict) or not isinstance(manifest.get("artifact_dir"), str):
        raise ManualEvidenceError("manual_portfolio_factor_manifest_invalid")
    directory = _safe_path(manifest["artifact_dir"], source_root, "manual_portfolio_factor_outside_controlled_root")
    files = manifest.get("files")
    if not isinstance(files, list) or {item.get("role") for item in files if isinstance(item, dict)} != {
        "factor_values", "ic_series", "quantile_returns", "report",
    }:
        raise ManualEvidenceError("manual_portfolio_factor_manifest_invalid")
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise ManualEvidenceError("manual_portfolio_factor_manifest_invalid")
        _safe_path(item["path"], source_root, "manual_portfolio_factor_outside_controlled_root")
    return manifest, directory


def _factor_artifact_reverified(db: Session, artifact: ResearchEvidenceArtifact, source_root: Path) -> dict[str, Any]:
    if artifact.status != "verified" or artifact.kind not in {"factor_training", "factor_validation"}:
        raise ManualEvidenceError("manual_portfolio_factor_artifact_invalid")
    manifest, directory = _factor_scope(artifact, source_root)
    if not verify_research_artifact(artifact):
        raise ManualEvidenceError("manual_portfolio_factor_artifact_integrity_invalid")
    try:
        refreshed = _reverify_factor_artifact(db, artifact)
    except (ManualEvidenceError, KeyError, OSError, TypeError, ValueError) as exc:
        raise ManualEvidenceError("manual_portfolio_factor_artifact_reverification_failed") from exc
    if not refreshed or not verify_research_artifact(artifact):
        raise ManualEvidenceError("manual_portfolio_factor_artifact_reverification_failed")
    # _reverify_factor_artifact reads the path from the DB row.  The service
    # scope check above is what prevents that helper from widening the root.
    return {"artifact": artifact, "manifest": manifest, "directory": directory}


def _identity(artifact: ResearchEvidenceArtifact) -> dict[str, Any]:
    identity = _json(artifact.identity_json, "factor_identity")
    if not isinstance(identity, dict):
        raise ManualEvidenceError("manual_portfolio_factor_identity_invalid")
    return identity


def _parse_day(value: Any, field: str) -> date:
    try:
        if isinstance(value, datetime):
            return value.date()
        return value if isinstance(value, date) else date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ManualEvidenceError(f"manual_portfolio_{field}_invalid") from exc


def _validate_factor_lineage(
    pair: dict[str, Any],
    *,
    training: ResearchEvidenceArtifact,
    validation: ResearchEvidenceArtifact,
    source_root: Path,
) -> None:
    train = _identity(training)
    valid = _identity(validation)
    required = (
        "candidate_id", "dataset_id", "data_content_hash", "label_spec",
        "evaluation_policy", "role", "direction", "expression_hash",
    )
    if any(key not in train or key not in valid for key in required):
        raise ManualEvidenceError("manual_portfolio_factor_identity_incomplete")
    if any(train[key] != valid[key] for key in required):
        raise ManualEvidenceError("manual_portfolio_factor_lineage_mismatch")
    if (
        train["role"] != "rank"
        or isinstance(train["direction"], bool)
        or train["direction"] not in {-1, 1}
    ):
        raise ManualEvidenceError("manual_portfolio_factor_role_not_rank")
    if train["label_spec"] != _MANUAL_LABEL:
        raise ManualEvidenceError("manual_portfolio_factor_label_mismatch")
    if train.get("decision") != "training_passed" or valid.get("decision") != "validation_passed":
        raise ManualEvidenceError("manual_portfolio_factor_gate_not_passed")
    if _parse_day(train["end_date"], "training_end") >= _parse_day(valid["start_date"], "validation_start"):
        raise ManualEvidenceError("manual_portfolio_factor_period_overlap")

    for scenario in _SCENARIOS:
        result = pair[scenario].result
        bundle = result.bundle
        if bundle.factor_expression_hash != train["expression_hash"]:
            raise ManualEvidenceError("manual_portfolio_bundle_expression_mismatch")
        if bundle.training_evidence_hash != training.evidence_hash or bundle.validation_evidence_hash != validation.evidence_hash:
            raise ManualEvidenceError("manual_portfolio_bundle_evidence_mismatch")
        if bundle.dataset_content_hash != train["data_content_hash"]:
            raise ManualEvidenceError("manual_portfolio_bundle_dataset_mismatch")
        if bundle.label_spec_hash != stable_hash(MANUAL_DAILY_LABEL_V1.as_dict()):
            raise ManualEvidenceError("manual_portfolio_bundle_label_hash_mismatch")
        manifest = result.input_manifest
        if (
            manifest.training_artifact_id != training.id
            or manifest.validation_artifact_id != validation.id
            or manifest.training_artifact_hash != training.evidence_hash
            or manifest.validation_artifact_hash != validation.evidence_hash
        ):
            raise ManualEvidenceError("manual_portfolio_input_evidence_mismatch")

    valid_start = _parse_day(valid["start_date"], "validation_start")
    valid_end = _parse_day(valid["end_date"], "validation_end")
    validation_manifest, _ = _factor_scope(validation, source_root)
    values_path = next(
        (Path(item["path"]) for item in validation_manifest["files"] if item.get("role") == "factor_values"),
        None,
    )
    if values_path is None:
        raise ManualEvidenceError("manual_portfolio_factor_values_missing")
    values_path = _safe_path(values_path, source_root, "manual_portfolio_factor_outside_controlled_root")
    try:
        table = pq.read_table(values_path)
    except (OSError, ValueError, TypeError) as exc:
        raise ManualEvidenceError("manual_portfolio_factor_values_invalid") from exc
    if set(table.column_names) != {"date", "code", "factor"}:
        raise ManualEvidenceError("manual_portfolio_factor_values_schema_invalid")
    factor_rows: dict[tuple[date, str], Decimal] = {}
    seen_factor_keys: set[tuple[date, str]] = set()
    for row in table.to_pylist():
        day = _parse_day(row.get("date"), "factor_date")
        code = str(row.get("code") or "").strip().upper()
        raw_factor = row.get("factor")
        if not code or not (valid_start <= day <= valid_end):
            raise ManualEvidenceError("manual_portfolio_factor_values_invalid")
        key = (day, code)
        if key in seen_factor_keys:
            raise ManualEvidenceError("manual_portfolio_factor_values_duplicate")
        seen_factor_keys.add(key)
        if raw_factor is None:
            continue
        try:
            factor = Decimal(str(raw_factor))
        except (ArithmeticError, TypeError, ValueError) as exc:
            raise ManualEvidenceError("manual_portfolio_factor_values_nonfinite") from exc
        if factor.is_nan():
            continue
        if not factor.is_finite():
            raise ManualEvidenceError("manual_portfolio_factor_values_nonfinite")
        factor_rows[key] = factor
    if not factor_rows:
        raise ManualEvidenceError("manual_portfolio_factor_values_empty")
    direction = int(train["direction"])
    # Every finite persisted result signal must come from the validation
    # factor artifact.  This intentionally rejects an output that adds a
    # training or tail signal to improve its portfolio result.
    calendar_entry = next((item for item in pair["baseline"].result.input_manifest.source_files if item.get("role") == "calendar"), None)
    if not isinstance(calendar_entry, dict) or not isinstance(calendar_entry.get("path"), str):
        raise ManualEvidenceError("manual_portfolio_calendar_source_missing")
    calendar_path = _safe_path(calendar_entry["path"], source_root, "manual_portfolio_calendar_outside_controlled_root")
    try:
        calendar_table = pq.read_table(calendar_path)
        calendar_days = [_parse_day(value, "calendar_date") for value in calendar_table.column("date").to_pylist()]
    except (OSError, TypeError, ValueError) as exc:
        raise ManualEvidenceError("manual_portfolio_calendar_source_invalid") from exc
    if not calendar_days or any(left >= right for left, right in zip(calendar_days, calendar_days[1:])):
        raise ManualEvidenceError("manual_portfolio_calendar_source_invalid")
    valid_sessions = [day for day in calendar_days if valid_start <= day <= valid_end]
    if not valid_sessions:
        raise ManualEvidenceError("manual_portfolio_calendar_source_invalid")
    required_start = valid_sessions[0]
    finite_dates = sorted({key[0] for key in factor_rows})
    last_index = calendar_days.index(finite_dates[-1]) if finite_dates[-1] in calendar_days else -1
    if last_index < 0 or last_index + 2 >= len(calendar_days):
        raise ManualEvidenceError("manual_portfolio_validation_exit_out_of_calendar")
    required_end = calendar_days[last_index + 2]
    for scenario in _SCENARIOS:
        rows: dict[tuple[date, str], Decimal] = {}
        for row in pair[scenario].result.signals:
            day = _parse_day(row["signal_date"], "signal_date")
            code = str(row["code"] or "").strip().upper()
            try:
                score = Decimal(str(row["score"])).quantize(_SCORE_QUANTUM, rounding=ROUND_HALF_EVEN)
            except (ArithmeticError, TypeError, ValueError) as exc:
                raise ManualEvidenceError("manual_portfolio_signal_score_invalid") from exc
            if not code or not score.is_finite():
                raise ManualEvidenceError("manual_portfolio_signal_score_invalid")
            key = (day, code)
            if key in rows:
                raise ManualEvidenceError("manual_portfolio_signal_duplicate")
            rows[key] = score
        if set(rows) != set(factor_rows):
            raise ManualEvidenceError("manual_portfolio_validation_signal_coverage_mismatch")
        for key, value in factor_rows.items():
            expected = (value * direction).quantize(_SCORE_QUANTUM, rounding=ROUND_HALF_EVEN)
            if rows[key] != expected:
                raise ManualEvidenceError("manual_portfolio_validation_signal_value_mismatch")
        if pair[scenario].result.start_date != required_start or pair[scenario].result.end_date < required_end:
            raise ManualEvidenceError("manual_portfolio_validation_window_incomplete")


def _portfolio_manifest(artifact: Any) -> dict[str, Any]:
    directory = artifact.directory.resolve(strict=True)
    files = []
    by_name = {item.get("path"): item for item in artifact.manifest.get("files", []) if isinstance(item, dict)}
    for name in _PORTFOLIO_FILES:
        if name == "manifest.json":
            path = directory / name
            files.append({"role": "manifest", "path": str(path), "size": path.stat().st_size, "sha256": _file_hash(path)})
            continue
        item = by_name.get(name)
        if item is None:
            raise ManualEvidenceError("manual_portfolio_manifest_file_missing")
        expected_role = name.removesuffix(".json").removesuffix(".parquet")
        if item.get("role") != expected_role:
            raise ManualEvidenceError("manual_portfolio_manifest_file_role_invalid")
        path = directory / name
        files.append({"role": expected_role, "path": str(path), "size": path.stat().st_size, "sha256": _file_hash(path)})
    return {"artifact_dir": str(directory), "files": files}


def _registered_manifest_scope(manifest: dict[str, Any], allowed_root: Path) -> Path:
    if not isinstance(manifest.get("artifact_dir"), str) or not isinstance(manifest.get("files"), list):
        raise ManualEvidenceError("manual_portfolio_registered_manifest_invalid")
    directory = _safe_path(manifest["artifact_dir"], allowed_root, "manual_portfolio_artifact_outside_controlled_root")
    if len(manifest["files"]) != len(_PORTFOLIO_FILES):
        raise ManualEvidenceError("manual_portfolio_registered_manifest_invalid")
    seen = set()
    for item in manifest["files"]:
        if not isinstance(item, dict) or set(item) != {"role", "path", "size", "sha256"}:
            raise ManualEvidenceError("manual_portfolio_registered_manifest_invalid")
        role = item["role"]
        if role in seen or role not in {name.removesuffix(".json").removesuffix(".parquet") for name in _PORTFOLIO_FILES}:
            raise ManualEvidenceError("manual_portfolio_registered_manifest_invalid")
        seen.add(role)
        path = _safe_path(item["path"], allowed_root, "manual_portfolio_artifact_outside_controlled_root")
        if path.parent != directory or path.name != f"{role}.parquet" and path.name != f"{role}.json":
            raise ManualEvidenceError("manual_portfolio_registered_manifest_invalid")
    if len(seen) != len(_PORTFOLIO_FILES):
        raise ManualEvidenceError("manual_portfolio_registered_manifest_invalid")
    return directory


def _db_payload(pair: dict[str, Any], scenario: str, training: ResearchEvidenceArtifact, validation: ResearchEvidenceArtifact) -> tuple[dict[str, Any], dict[str, Any]]:
    artifact = pair[scenario]
    identity = {
        "pair_hash": pair["pair_hash"], "scenario": scenario,
        "strategy_core_hash": artifact.result.bundle.strategy_core_hash,
        "bundle_hash": artifact.result.bundle.bundle_hash,
        "input_manifest_hash": artifact.result.input_manifest.manifest_hash,
        "training_artifact_id": training.id, "training_evidence_hash": training.evidence_hash,
        "validation_artifact_id": validation.id, "validation_evidence_hash": validation.evidence_hash,
        "manifest_file_sha256": artifact.manifest_file_sha256,
    }
    manifest = _portfolio_manifest(artifact)
    return identity, manifest


def _existing_match(row: ResearchEvidenceArtifact, identity: dict[str, Any], manifest: dict[str, Any]) -> bool:
    scenario = identity.get("scenario")
    if (
        row.kind != _PORTFOLIO_KINDS.get(scenario)
        or row.producer_protocol != PRODUCER_PROTOCOL
        or row.producer_entity_type != ENTITY_TYPE
        or row.producer_entity_id != identity.get("pair_hash")
        or row.producer_attempt != 1
    ):
        raise ManualEvidenceError("manual_portfolio_existing_artifact_conflict")
    if row.status != "verified":
        raise ManualEvidenceError("manual_portfolio_existing_artifact_invalidated")
    expected_identity_hash = _hash(identity)
    expected_manifest_hash = _hash(manifest)
    expected_evidence_hash = _hash({"identity_hash": expected_identity_hash, "manifest_hash": expected_manifest_hash})
    if (
        _json(row.identity_json, "portfolio_identity") != identity
        or _json(row.manifest_json, "portfolio_manifest") != manifest
        or row.identity_hash != expected_identity_hash
        or row.manifest_hash != expected_manifest_hash
        or row.evidence_hash != expected_evidence_hash
    ):
        raise ManualEvidenceError("manual_portfolio_existing_artifact_conflict")
    return True


def _audit_and_validate(
    db: Session,
    baseline_dir: str | Path,
    stress_dir: str | Path,
    *,
    allowed_root: Path,
    source_root: Path,
) -> tuple[dict[str, Any], ResearchEvidenceArtifact, ResearchEvidenceArtifact, dict[str, Any], dict[str, Any]]:
    try:
        pair = audit_portfolio_pair(baseline_dir, stress_dir, allowed_root=allowed_root, source_root=source_root)
    except (OSError, TypeError, ValueError) as exc:
        raise ManualEvidenceError("manual_portfolio_pair_audit_failed") from exc
    base_result = pair["baseline"].result
    training_id = base_result.input_manifest.training_artifact_id
    validation_id = base_result.input_manifest.validation_artifact_id
    training = db.get(ResearchEvidenceArtifact, training_id)
    validation = db.get(ResearchEvidenceArtifact, validation_id)
    if training is None or validation is None:
        raise ManualEvidenceError("manual_portfolio_factor_artifact_not_found")
    if training.kind != "factor_training" or validation.kind != "factor_validation":
        raise ManualEvidenceError("manual_portfolio_factor_artifact_kind_invalid")
    _factor_artifact_reverified(db, training, source_root)
    _factor_artifact_reverified(db, validation, source_root)
    _validate_factor_lineage(pair, training=training, validation=validation, source_root=source_root)
    identities = {}
    manifests = {}
    for scenario in _SCENARIOS:
        identities[scenario], manifests[scenario] = _db_payload(pair, scenario, training, validation)
    # The audit performs its own final mutation check.  Repeat the manifest
    # byte check immediately before constructing the DB payload so a swap
    # between audit and registration cannot be bound to the old pair hash.
    for scenario in _SCENARIOS:
        try:
            final = verify_portfolio_evidence_directory(pair[scenario].directory)
        except (OSError, TypeError, ValueError) as exc:
            raise ManualEvidenceError("manual_portfolio_artifact_changed_during_read") from exc
        if final["manifest_file_sha256"] != pair[scenario].manifest_file_sha256:
            raise ManualEvidenceError("manual_portfolio_artifact_changed_during_read")
    return pair, training, validation, identities, manifests


def register_manual_portfolio_pair(
    db: Session,
    baseline_dir: str | Path,
    stress_dir: str | Path,
    *,
    allowed_root: str | Path | None = None,
    source_root: str | Path | None = None,
    commit: bool = True,
) -> dict[str, str]:
    """Audit, lineage-check and atomically register one baseline/stress pair."""
    allowed = _root(allowed_root, Path(settings.result_dir))
    source = _root(source_root, Path(settings.result_dir).parent)
    pair, training, validation, identities, manifests = _audit_and_validate(
        db, baseline_dir, stress_dir, allowed_root=allowed, source_root=source,
    )
    rows: dict[str, ResearchEvidenceArtifact] = {}
    with db.no_autoflush:
        for scenario in _SCENARIOS:
            rows[scenario] = db.scalars(select(ResearchEvidenceArtifact).where(
                ResearchEvidenceArtifact.kind == _PORTFOLIO_KINDS[scenario],
                ResearchEvidenceArtifact.producer_protocol == PRODUCER_PROTOCOL,
                ResearchEvidenceArtifact.producer_entity_type == ENTITY_TYPE,
                ResearchEvidenceArtifact.producer_entity_id == pair["pair_hash"],
                ResearchEvidenceArtifact.producer_attempt == 1,
            )).first()
        if any(rows.values()) and not all(rows.values()):
            raise ManualEvidenceError("manual_portfolio_pair_half_registered")
        if all(rows.values()):
            for scenario in _SCENARIOS:
                _existing_match(rows[scenario], identities[scenario], manifests[scenario])
            return {"pair_hash": pair["pair_hash"], "baseline_artifact_id": rows["baseline"].id, "stress_artifact_id": rows["stress"].id}
        current_dirs = {
            manifests[scenario]["artifact_dir"]: scenario for scenario in _SCENARIOS
        }
        registered = db.scalars(select(ResearchEvidenceArtifact).where(
            ResearchEvidenceArtifact.kind.in_(tuple(_PORTFOLIO_KINDS.values())),
            ResearchEvidenceArtifact.producer_protocol == PRODUCER_PROTOCOL,
        )).all()
        for row in registered:
            old_manifest = _json(row.manifest_json, "portfolio_manifest")
            scenario = current_dirs.get(old_manifest.get("artifact_dir"))
            if scenario is None or row.producer_entity_id == pair["pair_hash"]:
                continue
            if row.status != "verified":
                raise ManualEvidenceError("manual_portfolio_existing_artifact_invalidated")
            old_identity = _identity(row)
            if (
                old_identity.get("scenario") != scenario
                or old_identity.get("manifest_file_sha256") != identities[scenario]["manifest_file_sha256"]
            ):
                raise ManualEvidenceError("manual_portfolio_existing_artifact_conflict")
    # Flush caller work before opening the pair savepoint, so a failed pair
    # cannot erase unrelated pending rows in the caller's transaction.
    try:
        db.flush()
        with db.begin_nested():
            for scenario in _SCENARIOS:
                identity, manifest = identities[scenario], manifests[scenario]
                row = ResearchEvidenceArtifact(
                    id=uuid4_str(), kind=_PORTFOLIO_KINDS[scenario],
                    producer_protocol=PRODUCER_PROTOCOL, producer_entity_type=ENTITY_TYPE,
                    producer_entity_id=pair["pair_hash"], producer_attempt=1, status="verified",
                    identity_json=json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    identity_hash=_hash(identity), manifest_json=json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    manifest_hash=_hash(manifest), evidence_hash=_hash({"identity_hash": _hash(identity), "manifest_hash": _hash(manifest)}),
                )
                db.add(row)
                db.flush()
                rows[scenario] = row
    except IntegrityError as exc:
        raise ManualEvidenceError("manual_portfolio_artifact_unique_conflict") from exc
    except (OSError, TypeError, ValueError) as exc:
        raise ManualEvidenceError("manual_portfolio_pair_registration_failed") from exc
    if commit:
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise ManualEvidenceError("manual_portfolio_artifact_unique_conflict") from exc
    return {"pair_hash": pair["pair_hash"], "baseline_artifact_id": rows["baseline"].id, "stress_artifact_id": rows["stress"].id}


def reverify_registered_portfolio_pair(
    db: Session,
    baseline_artifact_id: str,
    stress_artifact_id: str,
    *,
    allowed_root: str | Path | None = None,
    source_root: str | Path | None = None,
) -> dict[str, Any]:
    """Re-read an existing pair and return the independent audit result."""
    allowed = _root(allowed_root, Path(settings.result_dir))
    source = _root(source_root, Path(settings.result_dir).parent)
    base = db.get(ResearchEvidenceArtifact, baseline_artifact_id)
    stress = db.get(ResearchEvidenceArtifact, stress_artifact_id)
    if base is None or stress is None:
        raise ManualEvidenceError("manual_portfolio_registered_artifact_not_found")
    if base.kind != _PORTFOLIO_KINDS["baseline"] or stress.kind != _PORTFOLIO_KINDS["stress"]:
        raise ManualEvidenceError("manual_portfolio_registered_artifact_kind_invalid")
    if base.status != "verified" or stress.status != "verified":
        raise ManualEvidenceError("manual_portfolio_registered_artifact_invalidated")
    base_manifest = _json(base.manifest_json, "portfolio_manifest")
    stress_manifest = _json(stress.manifest_json, "portfolio_manifest")
    base_identity = _identity(base)
    stress_identity = _identity(stress)
    if (
        base_identity.get("scenario") != "baseline"
        or stress_identity.get("scenario") != "stress"
        or base_identity.get("pair_hash") != stress_identity.get("pair_hash")
    ):
        raise ManualEvidenceError("manual_portfolio_registered_pair_mismatch")
    _existing_match(base, base_identity, base_manifest)
    _existing_match(stress, stress_identity, stress_manifest)
    base_dir = _registered_manifest_scope(base_manifest, allowed)
    stress_dir = _registered_manifest_scope(stress_manifest, allowed)
    if not verify_research_artifact(base) or not verify_research_artifact(stress):
        raise ManualEvidenceError("manual_portfolio_registered_artifact_integrity_invalid")
    pair, training, validation, identities, manifests = _audit_and_validate(
        db, base_dir, stress_dir, allowed_root=allowed, source_root=source,
    )
    if identities["baseline"]["pair_hash"] != identities["stress"]["pair_hash"] or pair["pair_hash"] != identities["baseline"]["pair_hash"]:
        raise ManualEvidenceError("manual_portfolio_registered_pair_mismatch")
    _existing_match(base, identities["baseline"], manifests["baseline"])
    _existing_match(stress, identities["stress"], manifests["stress"])
    return pair


__all__ = ["register_manual_portfolio_pair", "reverify_registered_portfolio_pair"]
