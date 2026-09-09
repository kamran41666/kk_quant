"""Persistent, deterministic factor research queue and worker implementation."""
from __future__ import annotations

from datetime import date, datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from quant_engine.factor import compute_factor, get_factor_definition
from quant_engine.factor.evaluation import calc_ic, calc_ic_summary, quantile_analysis
from quant_engine.factor.expression import FactorExpressionSpec
from quant_engine.factor.gates import FactorGatePolicy, apply_training_gates
from quant_engine.factor.generation import GENERATORS
from quant_engine.factor.operators import to_wide
from server.config import settings
from server.models.schema import FactorCandidate, FactorExperiment


BASELINE_FACTORS = (
    "raw_momentum_lagged_20_close",
    "raw_realized_volatility_20_close",
    "raw_turnover_mean_20_turnover",
)


def _now() -> datetime:
    return datetime.now()


def _json_number(value: Any) -> float | int | None:
    if isinstance(value, (int, np.integer)):
        return int(value)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _spec_payload(spec: FactorExpressionSpec) -> dict[str, Any]:
    payload = spec.as_dict()
    for derived in ("expression_hash", "required_fields", "lookback"):
        payload.pop(derived, None)
    return payload


def serialize_candidate(row: FactorCandidate) -> dict[str, Any]:
    spec = FactorExpressionSpec.from_dict(json.loads(row.expression_spec))
    return {
        "id": row.id,
        "name": row.name,
        "expression_hash": row.expression_hash,
        "spec": spec.as_dict(),
        "status": row.status,
        "rejection_reason": row.rejection_reason,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def serialize_experiment(row: FactorExperiment) -> dict[str, Any]:
    return {
        "id": row.id,
        "candidate_id": row.candidate_id,
        "dataset_id": row.dataset_id,
        "data_content_hash": row.data_content_hash,
        "start_date": row.start_date,
        "end_date": row.end_date,
        "forward_horizon": row.forward_horizon,
        "stage": row.stage,
        "evaluation_policy": FactorGatePolicy.from_dict(
            json.loads(row.evaluation_policy or "{}")
        ).as_dict(),
        "status": row.status,
        "attempt": row.attempt,
        "lease_owner": row.lease_owner,
        "lease_until": row.lease_until,
        "result": json.loads(row.result_json) if row.result_json else None,
        "artifact_dir": row.artifact_dir,
        "error_code": row.error_code,
        "error_message": row.error_message,
        "created_at": row.created_at,
        "started_at": row.started_at,
        "completed_at": row.completed_at,
        "updated_at": row.updated_at,
    }


def register_candidate(
    db: Session,
    payload: Mapping[str, Any],
) -> tuple[FactorCandidate, bool]:
    spec = FactorExpressionSpec.from_dict(payload)
    existing = db.query(FactorCandidate).filter(
        FactorCandidate.expression_hash == spec.expression_hash
    ).first()
    if existing is not None:
        if existing.direction != spec.direction or existing.role != spec.role:
            raise ValueError(
                "factor expression is already registered with a different direction or role"
            )
        return existing, False
    row = FactorCandidate(
        name=spec.name,
        expression_hash=spec.expression_hash,
        expression_spec=json.dumps(
            _spec_payload(spec), ensure_ascii=False, sort_keys=True, allow_nan=False
        ),
        hypothesis=spec.hypothesis,
        direction=spec.direction,
        role=spec.role,
        source=spec.source,
        parent_hash=spec.parent_hash,
        status="registered",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row, True


def list_candidates(db: Session, *, limit: int = 100) -> list[dict[str, Any]]:
    rows = db.query(FactorCandidate).order_by(
        FactorCandidate.created_at.desc(), FactorCandidate.id.desc()
    ).limit(limit).all()
    return [serialize_candidate(row) for row in rows]


def generate_candidates(
    db: Session,
    *,
    generator_name: str = "template-v1",
    limit: int = 4,
) -> dict[str, Any]:
    generator = GENERATORS.get(generator_name)
    if generator is None:
        raise KeyError(f"factor candidate generator not found: {generator_name}")
    memory = research_memory(db, limit=500)
    registered = [
        {"expression_hash": value}
        for (value,) in db.query(FactorCandidate.expression_hash).all()
    ]
    generator_memory = {**memory, "items": [*memory["items"], *registered]}
    generated = generator.generate(memory=generator_memory, limit=limit)
    rows = []
    for spec in generated:
        row, created = register_candidate(db, _spec_payload(spec))
        rows.append({"created": created, "candidate": serialize_candidate(row)})
    return {
        "generator": generator_name,
        "requested": limit,
        "generated": len(rows),
        "candidates": rows,
        "memory_counts": memory["counts"],
    }


def _manifest_index(data_root: Path) -> dict[str, tuple[Path, dict[str, Any]]]:
    result: dict[str, tuple[Path, dict[str, Any]]] = {}
    research_root = data_root / "research"
    if not research_root.exists():
        return result
    for path in research_root.rglob("manifest.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            dataset_id = str(payload["dataset_id"])
            content_hash = str(payload["content_hash"])
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if len(content_hash) == 64:
            if dataset_id in result and result[dataset_id][1]["content_hash"] != content_hash:
                raise ValueError(f"duplicate dataset_id has conflicting content: {dataset_id}")
            result[dataset_id] = (path, payload)
    return result


def queue_experiment(
    db: Session,
    *,
    candidate_id: str,
    dataset_id: str,
    start: date,
    end: date,
    forward_horizon: int = 5,
    stage: str = "training",
    evaluation_policy: Mapping[str, Any] | None = None,
    data_root: Path | None = None,
) -> tuple[FactorExperiment, bool]:
    if start > end:
        raise ValueError("factor experiment start must not exceed end")
    if not 1 <= forward_horizon <= 60:
        raise ValueError("factor experiment forward_horizon must be in [1, 60]")
    if stage not in {"training", "validation", "holdout"}:
        raise ValueError("factor experiment stage must be training, validation or holdout")
    candidate = db.get(FactorCandidate, candidate_id)
    if candidate is None:
        raise KeyError(f"factor candidate not found: {candidate_id}")
    policy = FactorGatePolicy.from_dict(evaluation_policy)
    policy_json = json.dumps(policy.as_dict(), sort_keys=True, allow_nan=False)
    manifests = _manifest_index(data_root or Path(settings.data_dir))
    if dataset_id not in manifests:
        raise KeyError(f"registered frozen research dataset not found: {dataset_id}")
    _, manifest = manifests[dataset_id]
    available_start = date.fromisoformat(str(manifest["start_date"]))
    available_end = date.fromisoformat(str(manifest["end_date"]))
    if start < available_start or end > available_end:
        raise ValueError("factor experiment dates exceed the frozen dataset range")
    prerequisite = {
        "validation": ("training", "training_passed"),
        "holdout": ("validation", "validation_passed"),
    }.get(stage)
    if prerequisite is not None:
        required_stage, required_decision = prerequisite
        prior = db.query(FactorExperiment).filter(
            FactorExperiment.candidate_id == candidate_id,
            FactorExperiment.dataset_id == dataset_id,
            FactorExperiment.data_content_hash == str(manifest["content_hash"]),
            FactorExperiment.forward_horizon == forward_horizon,
            FactorExperiment.stage == required_stage,
            FactorExperiment.status == "completed",
        ).all()
        matched = []
        for item in prior:
            if not item.result_json or date.fromisoformat(item.end_date) >= start:
                continue
            prior_policy = FactorGatePolicy.from_dict(
                json.loads(item.evaluation_policy or "{}")
            )
            if prior_policy != policy:
                continue
            if json.loads(item.result_json).get("decision") == required_decision:
                matched.append(item)
        if not matched:
            raise ValueError(
                f"factor experiment stage {stage} requires earlier, matching "
                f"{required_decision} evidence"
            )
    existing = db.query(FactorExperiment).filter(
        FactorExperiment.candidate_id == candidate_id,
        FactorExperiment.dataset_id == dataset_id,
        FactorExperiment.start_date == start.isoformat(),
        FactorExperiment.end_date == end.isoformat(),
        FactorExperiment.forward_horizon == forward_horizon,
    ).first()
    if existing is not None:
        if existing.data_content_hash != str(manifest["content_hash"]):
            raise ValueError("frozen dataset_id content changed after the earlier experiment")
        if existing.stage != stage:
            raise ValueError("factor experiment identity already exists with a different stage")
        existing_policy = FactorGatePolicy.from_dict(
            json.loads(existing.evaluation_policy or "{}")
        )
        if existing_policy != policy:
            raise ValueError("factor experiment identity already exists with a different gate policy")
        return existing, False
    row = FactorExperiment(
        candidate_id=candidate_id,
        dataset_id=dataset_id,
        data_content_hash=str(manifest["content_hash"]),
        start_date=start.isoformat(),
        end_date=end.isoformat(),
        forward_horizon=forward_horizon,
        stage=stage,
        evaluation_policy=policy_json,
        status="queued",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row, True


def list_experiments(db: Session, *, limit: int = 100) -> list[dict[str, Any]]:
    rows = db.query(FactorExperiment).order_by(
        FactorExperiment.created_at.desc(), FactorExperiment.id.desc()
    ).limit(limit).all()
    return [serialize_experiment(row) for row in rows]


def research_memory(db: Session, *, limit: int = 100) -> dict[str, Any]:
    """Compact evaluated/failed evidence for the next candidate-generation round."""
    rows = db.query(FactorExperiment).filter(
        FactorExperiment.stage == "training",
        FactorExperiment.status.in_(("completed", "failed"))
    ).order_by(FactorExperiment.updated_at.desc(), FactorExperiment.id.desc()).limit(limit).all()
    items = []
    counts = {
        "training_passed": 0,
        "training_rejected": 0,
        "legacy_evaluated": 0,
        "failed": 0,
    }
    for row in rows:
        candidate = db.get(FactorCandidate, row.candidate_id)
        result = json.loads(row.result_json) if row.result_json else None
        decision = result.get("decision") if result else "failed"
        if decision == "evaluated_not_promoted":
            decision = "legacy_evaluated"
        if decision not in counts:
            decision = "failed"
        counts[decision] += 1
        failed_gates = [
            gate["name"] for gate in (result or {}).get("gate_results", [])
            if not gate.get("passed")
        ]
        items.append({
            "experiment_id": row.id,
            "candidate_id": row.candidate_id,
            "name": candidate.name if candidate else None,
            "expression_hash": candidate.expression_hash if candidate else None,
            "dataset_id": row.dataset_id,
            "period": [row.start_date, row.end_date],
            "forward_horizon": row.forward_horizon,
            "decision": decision,
            "failed_gates": failed_gates,
            "coverage": (result or {}).get("coverage"),
            "directional_ic": (result or {}).get("directional_ic"),
            "maximum_absolute_correlation": (
                result or {}
            ).get("maximum_absolute_correlation"),
            "error_code": row.error_code,
        })
    return {
        "protocol_version": "factor-memory-v1",
        "counts": counts,
        "items": items,
        "limitations": [
            "Memory contains compact training evidence only.",
            "Final holdout results must not be supplied to a candidate generator.",
        ],
    }


def claim_next_experiment(
    db: Session,
    *,
    worker_id: str,
    lease_seconds: int = 300,
) -> FactorExperiment | None:
    if not worker_id or len(worker_id) > 80:
        raise ValueError("factor worker_id is required and limited to 80 characters")
    if not 10 <= lease_seconds <= 3600:
        raise ValueError("factor lease_seconds must be in [10, 3600]")
    now = _now()
    now_text = now.isoformat()
    for _ in range(3):
        row = db.query(FactorExperiment).filter(
            (FactorExperiment.status == "queued")
            | (
                (FactorExperiment.status == "running")
                & (FactorExperiment.lease_until < now_text)
            )
        ).order_by(FactorExperiment.created_at, FactorExperiment.id).first()
        if row is None:
            return None
        previous_status = row.status
        previous_lease = row.lease_until
        query = db.query(FactorExperiment).filter(
            FactorExperiment.id == row.id,
            FactorExperiment.status == previous_status,
        )
        if previous_status == "running":
            query = query.filter(FactorExperiment.lease_until == previous_lease)
        updated = query.update({
            "status": "running",
            "attempt": row.attempt + 1,
            "lease_owner": worker_id,
            "lease_until": (now + timedelta(seconds=lease_seconds)).isoformat(),
            "started_at": row.started_at or now_text,
            "updated_at": now_text,
            "error_code": None,
            "error_message": None,
        }, synchronize_session=False)
        if updated == 1:
            db.commit()
            return db.get(FactorExperiment, row.id)
        db.rollback()
    return None


def retry_experiment(db: Session, experiment_id: str) -> FactorExperiment:
    row = db.get(FactorExperiment, experiment_id)
    if row is None:
        raise KeyError(f"factor experiment not found: {experiment_id}")
    if row.status != "failed":
        raise ValueError("only a failed factor experiment can be retried")
    row.status = "queued"
    row.lease_owner = None
    row.lease_until = None
    row.error_code = None
    row.error_message = None
    row.completed_at = None
    row.updated_at = _now().isoformat()
    db.commit()
    db.refresh(row)
    return row


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_file(manifest_path: Path, item: Mapping[str, Any]) -> Path:
    configured = Path(str(item["path"]))
    candidates = (configured, manifest_path.parent / configured.name)
    path = next((candidate for candidate in candidates if candidate.exists()), None)
    if path is None:
        raise FileNotFoundError(f"frozen dataset file is missing: {configured.name}")
    if _sha256(path) != item["sha256"]:
        raise ValueError(f"frozen dataset file hash mismatch: {configured.name}")
    return path


def _research_panel(
    manifest_path: Path,
    manifest: Mapping[str, Any],
    fields: set[str],
    end: date,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    daily_path = _manifest_file(manifest_path, manifest["files"]["daily"])
    security_path = _manifest_file(manifest_path, manifest["files"]["securities"])
    required = {
        "code", "date", "open", "close", "adjusted_close", "is_suspended", "is_st",
        *fields,
    }
    daily = pd.read_parquet(daily_path, columns=sorted(required))
    daily["date"] = pd.to_datetime(daily["date"]).dt.date
    daily = daily[daily["date"] <= end].copy()
    securities = pd.read_parquet(
        security_path, columns=["code", "ipo_date", "out_date"]
    )
    securities["ipo_date"] = pd.to_datetime(securities["ipo_date"], errors="coerce").dt.date
    securities["out_date"] = pd.to_datetime(securities["out_date"], errors="coerce").dt.date
    daily = daily.merge(securities, on="code", how="left", validate="many_to_one")
    if daily["ipo_date"].isna().any():
        raise ValueError("frozen factor dataset contains securities without ipo_date")
    date_values = pd.to_datetime(daily["date"])
    ipo_values = pd.to_datetime(daily["ipo_date"])
    out_values = pd.to_datetime(daily["out_date"])
    active = date_values >= ipo_values
    active &= out_values.isna() | (date_values < out_values)
    active &= (date_values - ipo_values).dt.days >= 180
    eligible = active & ~daily["is_suspended"].astype(bool) & ~daily["is_st"].astype(bool)

    ratio = pd.to_numeric(daily["adjusted_close"], errors="coerce").div(
        pd.to_numeric(daily["close"], errors="coerce").where(daily["close"] > 0)
    )
    for field in fields | {"open"}:
        values = pd.to_numeric(daily[field], errors="coerce")
        if field in {"open", "high", "low", "close"}:
            values = values * ratio
        daily[field] = values.where(eligible)
    panel = daily.set_index(["code", "date"])[sorted(fields)].sort_index()
    open_wide = daily.set_index(["date", "code"])["open"].unstack("code").sort_index()
    return panel, open_wide


def _period(series: pd.Series, start: date, end: date) -> pd.Series:
    dates = series.index.get_level_values("date")
    return series.loc[(dates >= start) & (dates <= end)]


def _mean_daily_rank_correlation(
    candidate: pd.DataFrame,
    baseline: pd.DataFrame,
) -> float | None:
    left, right = candidate.align(baseline, join="inner")
    values = []
    for day in left.index:
        x, y = left.loc[day], right.loc[day]
        valid = x.notna() & y.notna()
        x, y = x[valid], y[valid]
        if len(x) < 3 or x.nunique() < 2 or y.nunique() < 2:
            continue
        correlation = x.rank().corr(y.rank())
        if pd.notna(correlation):
            values.append(float(correlation))
    return float(np.mean(values)) if values else None


def _ic_stability(ic_series: pd.Series, direction: int) -> dict[str, Any]:
    directional = ic_series.dropna().astype(float) * direction
    midpoint = len(directional) // 2
    first = directional.iloc[:midpoint]
    second = directional.iloc[midpoint:]
    first_mean = float(first.mean()) if len(first) else None
    second_mean = float(second.mean()) if len(second) else None
    consistent = (
        first_mean is not None and second_mean is not None
        and first_mean > 0 and second_mean > 0
    )
    return {
        "first_half_directional_ic": first_mean,
        "second_half_directional_ic": second_mean,
        "half_sign_consistent": consistent if first_mean is not None and second_mean is not None else None,
    }


def _quantile_summary(
    factor_wide: pd.DataFrame,
    labels: pd.DataFrame,
    direction: int,
) -> tuple[dict[str, Any], pd.DataFrame]:
    analysis = quantile_analysis(factor_wide * direction, labels, n_groups=5)
    group_returns = analysis["group_returns"]
    spread = analysis["top_bottom_spread"].dropna()
    means = {
        str(column): _json_number(group_returns[column].mean())
        for column in group_returns.columns
    }
    monotonicity = None
    finite_means = [means.get(f"Q{i}") for i in range(1, 6)]
    if all(value is not None for value in finite_means):
        monotonicity = _json_number(
            pd.Series(range(1, 6), dtype=float).corr(
                pd.Series(finite_means, dtype=float), method="spearman"
            )
        )
    return {
        "group_mean_returns": means,
        "directional_top_bottom_spread_mean": _json_number(spread.mean()) if len(spread) else None,
        "spread_positive_ratio": _json_number((spread > 0).mean()) if len(spread) else None,
        "monotonicity_spearman": monotonicity,
        "observation_count": len(group_returns),
    }, group_returns


def execute_experiment(
    db: Session,
    experiment_id: str,
    *,
    worker_id: str,
    data_root: Path | None = None,
    result_root: Path | None = None,
) -> dict[str, Any]:
    row = db.get(FactorExperiment, experiment_id)
    if row is None:
        raise KeyError(f"factor experiment not found: {experiment_id}")
    if row.status != "running" or row.lease_owner != worker_id:
        raise ValueError("factor experiment must be leased by this worker")
    candidate = db.get(FactorCandidate, row.candidate_id)
    if candidate is None:
        raise KeyError(f"factor candidate not found: {row.candidate_id}")
    try:
        manifests = _manifest_index(data_root or Path(settings.data_dir))
        manifest_path, manifest = manifests[row.dataset_id]
        if str(manifest["content_hash"]) != row.data_content_hash:
            raise ValueError("factor experiment dataset hash changed after queueing")
        spec = FactorExpressionSpec.from_dict(json.loads(candidate.expression_spec))
        policy = FactorGatePolicy.from_dict(json.loads(row.evaluation_policy or "{}"))
        baseline_definitions = {
            name: get_factor_definition(name) for name in BASELINE_FACTORS
        }
        fields = set(spec.required_fields)
        for definition in baseline_definitions.values():
            fields.update(definition.inputs)
        panel, open_wide = _research_panel(
            manifest_path,
            manifest,
            fields,
            date.fromisoformat(row.end_date),
        )
        start = date.fromisoformat(row.start_date)
        end = date.fromisoformat(row.end_date)
        factor = _period(spec.compute(panel[list(spec.required_fields)]), start, end)
        factor_frame = factor.to_frame("factor")
        factor_wide = to_wide(factor)
        # Match the existing label convention: T signal, T+1 open entry,
        # T+1+h open exit, without exposing labels to the expression.
        labels = open_wide.shift(-(row.forward_horizon + 1)).div(
            open_wide.shift(-1)
        ) - 1.0
        labels = labels.reindex(index=factor_wide.index, columns=factor_wide.columns)
        ic_series = calc_ic(factor_wide, labels, method="rank")
        ic_summary = {
            key: _json_number(value) for key, value in calc_ic_summary(ic_series).items()
        }
        finite = int(np.isfinite(factor.to_numpy(dtype=float)).sum())
        coverage = finite / len(factor) if len(factor) else 0.0
        correlations: dict[str, float | None] = {}
        for name, definition in baseline_definitions.items():
            baseline = _period(
                compute_factor(name, panel[list(definition.inputs)]), start, end
            )
            correlations[name] = _mean_daily_rank_correlation(
                factor_wide, to_wide(baseline)
            )
        finite_correlations = {
            name: abs(value) for name, value in correlations.items()
            if value is not None and math.isfinite(value)
        }
        closest = max(finite_correlations, key=finite_correlations.get) if finite_correlations else None
        maximum_correlation = finite_correlations.get(closest) if closest else None
        stability = _ic_stability(ic_series, spec.direction)
        quantiles, group_returns = _quantile_summary(
            factor_wide, labels, spec.direction
        )
        directional_ic = (
            float(ic_summary["ic_mean"]) * spec.direction
            if ic_summary["ic_mean"] is not None else None
        )
        training_decision, gate_results = apply_training_gates(
            policy,
            coverage=coverage,
            ic_observations=int(ic_summary["n_obs"] or 0),
            directional_ic=directional_ic,
            maximum_absolute_correlation=maximum_correlation,
            half_sign_consistent=stability["half_sign_consistent"],
            directional_quantile_spread=quantiles["directional_top_bottom_spread_mean"],
        )
        decision = training_decision
        if row.stage != "training":
            suffix = "passed" if training_decision == "training_passed" else "rejected"
            decision = f"{row.stage}_{suffix}"
        report = {
            "protocol_version": "factor-experiment-v2",
            "candidate_id": candidate.id,
            "candidate_name": candidate.name,
            "expression_hash": candidate.expression_hash,
            "dataset_id": row.dataset_id,
            "data_content_hash": row.data_content_hash,
            "start_date": row.start_date,
            "end_date": row.end_date,
            "forward_horizon": row.forward_horizon,
            "stage": row.stage,
            "forward_return_definition": "signal_t_close; entry_t_plus_1_open; exit_t_plus_1_plus_h_open",
            "row_count": len(factor),
            "finite_count": finite,
            "coverage": coverage,
            "ic_summary": ic_summary,
            "directional_ic": directional_ic,
            "ic_stability": stability,
            "baseline_correlations": correlations,
            "most_correlated_factor": closest,
            "maximum_absolute_correlation": maximum_correlation,
            "quantile_summary": quantiles,
            "evaluation_policy": policy.as_dict(),
            "gate_results": gate_results,
            "decision": decision,
            "limitations": [
                "Training gate success does not promote or authorize a strategy.",
                "IC significance is unadjusted for overlapping labels or multiple trials.",
                "The frozen 500-stock dataset is not a dynamic all-A-share universe.",
            ],
        }
        output_name = row.id if row.attempt == 1 else f"{row.id}-attempt{row.attempt}"
        output = (result_root or Path(settings.result_dir) / "factor-experiments") / output_name
        output.mkdir(parents=True, exist_ok=False)
        factor_frame.reset_index().to_parquet(output / "factor_values.parquet", index=False)
        ic_series.rename("rank_ic").to_frame().reset_index().to_parquet(
            output / "ic_series.parquet", index=False
        )
        group_returns.reset_index(names="date").to_parquet(
            output / "quantile_returns.parquet", index=False
        )
        (output / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
        completed = _now().isoformat()
        row.status = "completed"
        row.result_json = json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False)
        row.artifact_dir = str(output.resolve())
        row.lease_owner = None
        row.lease_until = None
        row.completed_at = completed
        row.updated_at = completed
        db.commit()
        candidate.status = decision
        candidate.rejection_reason = (
            ",".join(gate["name"] for gate in gate_results if not gate["passed"])
            if decision == "training_rejected" else None
        )
        candidate.updated_at = completed
        db.commit()
        return report
    except Exception as exc:
        failed = _now().isoformat()
        row.status = "failed"
        row.error_code = type(exc).__name__
        row.error_message = str(exc)[:2000]
        row.lease_owner = None
        row.lease_until = None
        row.completed_at = failed
        row.updated_at = failed
        db.commit()
        raise
