"""Read and independently audit persisted manual portfolio evidence.

No database promotion occurs here. Old v1 directories remain verifiable as
historical files, but lack the run envelope needed by this semantic reader.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from quant_engine.backtest.manual_daily_portfolio_v3 import (
    ManualDailyPortfolioV3Result,
    ManualPortfolioInputManifest,
)
from quant_engine.backtest.manual_portfolio_evidence import (
    _ARROW_SCHEMAS,
    _canonical,
    _decimal,
    _file_hash,
    _hash,
    calculate_portfolio_metrics,
    replay_portfolio_result,
    verify_portfolio_evidence_directory,
)
from quant_engine.backtest.manual_portfolio_sources import (
    load_verified_portfolio_sources,
)
from quant_engine.backtest.manual_research_ledger import RESEARCH_EXECUTION_COSTS
from quant_engine.factor.manual_daily_bundle import ManualDailyFactorBundleV2

ARTIFACT_VERSION = "manual-daily-portfolio-evidence-v2"
TABLE_ATTRIBUTES = {
    "signals.parquet": "signals", "order_intents.parquet": "intents",
    "order_attempts.parquet": "attempts", "trades.parquet": "trades",
    "corporate_actions.parquet": "corporate_actions", "positions.parquet": "positions",
    "daily_portfolio.parquet": "daily", "benchmark.parquet": "benchmark",
}


def controlled_path(path: str | Path, root: Path) -> Path:
    """Reject symlinks in every component before resolving a controlled path."""
    root = root.resolve(strict=True)
    candidate = Path(path)
    candidate = candidate if candidate.is_absolute() else root / candidate
    if ".." in candidate.parts:
        raise ValueError("portfolio_artifact_path_escape")
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("portfolio_artifact_path_escape") from exc
    cursor = root
    for part in relative.parts:
        cursor /= part
        if cursor.is_symlink():
            raise ValueError("portfolio_artifact_symlink")
    resolved = candidate.resolve(strict=True)
    if resolved != root and root not in resolved.parents:
        raise ValueError("portfolio_artifact_path_escape")
    return resolved


def artifact_code_hashes() -> dict[str, str]:
    root = Path(__file__).resolve().parents[2]
    names = (
        "backtest/manual_daily_portfolio_v3.py", "backtest/manual_research_ledger.py",
        "backtest/manual_research_inputs.py", "backtest/manual_portfolio_evidence.py",
        "backtest/manual_portfolio_sources.py", "backtest/manual_portfolio_artifacts.py",
        "factor/manual_daily_bundle.py", "trading/effective_rules.py",
    )
    return {name: _file_hash(root / "quant_engine" / name) for name in names}


def run_envelope(result: ManualDailyPortfolioV3Result) -> dict[str, Any]:
    capital = _decimal(result.initial_capital, "initial_capital")
    if capital <= 0 or capital != capital.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP):
        raise ValueError("portfolio_run_boundary_invalid")
    return {
        "protocol_version": "manual-daily-portfolio-v3",
        "start_date": result.start_date.isoformat(), "end_date": result.end_date.isoformat(),
        "initial_capital": str(capital),
        "terminal_policy": "require_closed_cohorts",
        "bundle": result.bundle.as_dict(),
        "cost_policy": {key: str(value) for key, value in RESEARCH_EXECUTION_COSTS[result.bundle.cost_scenario].items()},
        "code_hashes": artifact_code_hashes(),
    }


def reconstruct_result(
    envelope: dict[str, Any], input_payload: dict[str, Any],
    tables: dict[str, pa.Table], audit: dict[str, Any],
) -> ManualDailyPortfolioV3Result:
    if envelope.get("protocol_version") != "manual-daily-portfolio-v3":
        raise ValueError("portfolio_run_protocol_invalid")
    if envelope.get("terminal_policy") != "require_closed_cohorts":
        raise ValueError("portfolio_terminal_policy_invalid")
    bundle = ManualDailyFactorBundleV2.from_dict(envelope["bundle"])
    if _canonical(envelope["bundle"]) != _canonical(bundle.as_dict()):
        raise ValueError("portfolio_run_bundle_not_canonical")
    payload = dict(input_payload)
    if "source_files" in payload:
        payload["source_files"] = tuple(payload["source_files"])
    inputs = ManualPortfolioInputManifest(**payload)
    start, end = date.fromisoformat(envelope["start_date"]), date.fromisoformat(envelope["end_date"])
    capital = _decimal(envelope["initial_capital"], "initial_capital")
    if start > end or capital <= 0 or capital != capital.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP):
        raise ValueError("portfolio_run_boundary_invalid")
    if envelope["cost_policy"] != {
        key: str(value) for key, value in RESEARCH_EXECUTION_COSTS[bundle.cost_scenario].items()
    }:
        raise ValueError("portfolio_cost_policy_mismatch")
    rows = {}
    for filename, attribute in TABLE_ATTRIBUTES.items():
        table = tables[filename]
        if not table.schema.equals(_ARROW_SCHEMAS[filename], check_metadata=True):
            raise ValueError(f"portfolio_read_schema_mismatch:{filename}")
        rows[attribute] = [
            {key: value.isoformat() if isinstance(value, date) else str(value) if isinstance(value, Decimal) else value
             for key, value in row.items()}
            for row in table.to_pylist()
        ]
    cohorts = {}
    for row in rows["signals"]:
        if row["cohort_id"] is None:
            continue
        cohort = {
            "id": row["cohort_id"], "signal_date": row["signal_date"],
            "entry_date": row["entry_date"], "exit_date": row["planned_exit_date"],
            "budget": row["cohort_budget"], "status": row["cohort_terminal_status"],
        }
        if row["closed_date"] is not None:
            cohort["closed_date"] = row["closed_date"]
        if cohort["id"] in cohorts and cohorts[cohort["id"]] != cohort:
            raise ValueError("portfolio_cohort_rows_inconsistent")
        cohorts[cohort["id"]] = cohort
    return ManualDailyPortfolioV3Result(
        bundle=bundle, input_manifest=inputs, start_date=start, end_date=end,
        initial_capital=capital, cohorts=list(cohorts.values()),
        audit=audit["items"], quality_errors=audit["quality_errors"], **rows,
    )


@dataclass(frozen=True)
class AuditedPortfolioArtifact:
    directory: Path
    manifest: dict[str, Any]
    manifest_file_sha256: str
    result: ManualDailyPortfolioV3Result
    replay: dict[str, Any]
    metrics: dict[str, Any]


def read_portfolio_artifact(
    directory: str | Path, *, allowed_root: str | Path, source_root: str | Path,
) -> AuditedPortfolioArtifact:
    directory = controlled_path(directory, Path(allowed_root))
    verification = verify_portfolio_evidence_directory(directory)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("protocol_version") != ARTIFACT_VERSION:
        raise ValueError("portfolio_artifact_run_envelope_required")
    generated_at = manifest.get("generated_at")
    if not isinstance(generated_at, str):
        raise ValueError("portfolio_artifact_generated_at_invalid")  # noqa: TRY004
    try:
        parsed_generated_at = datetime.fromisoformat(generated_at)
    except ValueError as exc:
        raise ValueError("portfolio_artifact_generated_at_invalid") from exc
    if parsed_generated_at.tzinfo is None or parsed_generated_at.utcoffset() != timedelta(0):
        raise ValueError("portfolio_artifact_generated_at_not_utc")
    envelope = manifest.get("run_envelope")
    if not isinstance(envelope, dict):
        raise ValueError("portfolio_artifact_run_envelope_required")  # noqa: TRY004
    if envelope["code_hashes"] != artifact_code_hashes():
        raise ValueError("portfolio_artifact_code_version_mismatch")
    audit = json.loads((directory / "audit.json").read_text(encoding="utf-8"))
    result = reconstruct_result(
        envelope, manifest["input_manifest"],
        {name: pq.read_table(directory / name) for name in TABLE_ATTRIBUTES}, audit,
    )
    for actual, expected in (
        (result.result_hash, manifest["result_hash"]),
        (result.input_manifest.manifest_hash, manifest["input_manifest_hash"]),
        (result.bundle.strategy_core_hash, manifest["strategy_core_hash"]),
        (result.bundle.bundle_hash, manifest["scenario_bundle_hash"]),
        (result.bundle.cost_scenario, manifest["scenario"]),
        (result.input_manifest.dataset_content_hash, result.bundle.dataset_content_hash),
        (result.input_manifest.training_artifact_hash, result.bundle.training_evidence_hash),
        (result.input_manifest.validation_artifact_hash, result.bundle.validation_evidence_hash),
    ):
        if actual != expected:
            raise ValueError("portfolio_artifact_identity_mismatch")
    root = Path(source_root).resolve(strict=True)
    locator = manifest.get("source_locator")
    if not isinstance(locator, dict) or set(locator) != {"receipts", "signal_value_column"}:
        raise ValueError("portfolio_source_locator_required")
    receipts = locator["receipts"]
    if (
        not isinstance(receipts, list) or len(receipts) != 2
        or any(not isinstance(item, dict) for item in receipts)
        or {item.get("role") for item in receipts}
        != {"dataset_manifest", "benchmark_receipt"}
        or len({item.get("path") for item in receipts}) != 2
        or any(set(item) != {"role", "path", "size", "sha256"} for item in receipts)
    ):
        raise ValueError("portfolio_source_receipts_invalid")
    if not isinstance(locator["signal_value_column"], str) or not locator["signal_value_column"]:
        raise ValueError("portfolio_source_locator_required")
    paths = {}
    for receipt in receipts:
        if Path(receipt["path"]).is_absolute() or ".." in Path(receipt["path"]).parts:
            raise ValueError("portfolio_source_receipt_absolute")
        path = controlled_path(receipt["path"], root)
        if path.stat().st_size != receipt["size"] or _file_hash(path) != receipt["sha256"]:
            raise ValueError("portfolio_source_receipt_changed")
        paths[receipt["role"]] = path
    inputs = result.input_manifest
    for entry in inputs.source_files:
        if Path(entry["path"]).is_absolute() or ".." in Path(entry["path"]).parts:
            raise ValueError("portfolio_source_file_absolute")
        controlled_path(entry["path"], root)
    signal_entry = next(item for item in inputs.source_files if item["role"] == "signals")
    sources = load_verified_portfolio_sources(
        dataset_manifest_path=paths["dataset_manifest"], benchmark_receipt_path=paths["benchmark_receipt"],
        signal_path=controlled_path(signal_entry["path"], root), signal_sha256=inputs.signal_content_hash,
        training_artifact_id=inputs.training_artifact_id, training_artifact_hash=inputs.training_artifact_hash,
        validation_artifact_id=inputs.validation_artifact_id, validation_artifact_hash=inputs.validation_artifact_hash,
        allowed_root=root, signal_value_column=locator["signal_value_column"],
    )
    replay = replay_portfolio_result(result, sources)
    if not replay["passed"]:
        raise ValueError("portfolio_artifact_semantic_replay_failed")
    if any(row["status"] not in {"closed", "entry_failed"} for row in result.cohorts):
        raise ValueError("portfolio_artifact_unfinished_cohorts")
    if any(item.get("severity") in {"error", "P0", "P1"} for item in result.audit):
        raise ValueError("portfolio_artifact_quality_audit_failed")
    metrics = calculate_portfolio_metrics(result)
    summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    recorded_replay = json.loads((directory / "replay.json").read_text(encoding="utf-8"))
    if (
        _canonical(metrics) != _canonical(manifest["metrics"])
        or _canonical(metrics) != _canonical(summary["metrics"])
        or summary["result_hash"] != result.result_hash
        or summary.get("generator_result_hash") != manifest.get("generator_result_hash")
        or summary.get("accounting_replay_passed") != replay["accounting_passed"]
        or summary.get("source_and_execution_passed") != replay["source_and_execution_passed"]
        or summary.get("promotion_eligible") is not False
        or summary.get("evidence_status") != "unregistered"
        or manifest.get("eligible_for_artifact_registration")
        != (replay["passed"] and not result.quality_errors)
        or manifest["quality_errors"] != result.quality_errors
        or summary["quality_errors"] != result.quality_errors
        or recorded_replay != replay or manifest["replay_hash"] != replay["replay_hash"]
    ):
        raise ValueError("portfolio_artifact_derived_report_mismatch")
    # Recheck after parsing and replay, so mutation during the read is rejected.
    final = verify_portfolio_evidence_directory(directory)
    if final != verification or not sources.verify_files():
        raise ValueError("portfolio_artifact_changed_during_read")
    return AuditedPortfolioArtifact(directory, manifest, final["manifest_file_sha256"], result, replay, metrics)


def audit_portfolio_pair(
    baseline_dir: str | Path, stress_dir: str | Path, *,
    allowed_root: str | Path, source_root: str | Path,
) -> dict[str, Any]:
    baseline = read_portfolio_artifact(baseline_dir, allowed_root=allowed_root, source_root=source_root)
    stress = read_portfolio_artifact(stress_dir, allowed_root=allowed_root, source_root=source_root)
    if baseline.result.bundle.cost_scenario != "baseline" or stress.result.bundle.cost_scenario != "stress":
        raise ValueError("portfolio_pair_scenarios_invalid")
    def identity(artifact: AuditedPortfolioArtifact) -> dict[str, Any]:
        return {
            "strategy_core_hash": artifact.result.bundle.strategy_core_hash,
            "input_manifest_hash": artifact.result.input_manifest.manifest_hash,
            "start_date": artifact.result.start_date, "end_date": artifact.result.end_date,
            "initial_capital": artifact.result.initial_capital,
            "terminal_policy": artifact.manifest["run_envelope"]["terminal_policy"],
            "code_hashes": artifact.manifest["run_envelope"]["code_hashes"],
            "source_locator": artifact.manifest["source_locator"],
        }
    if identity(baseline) != identity(stress):
        raise ValueError("portfolio_pair_non_cost_identity_mismatch")
    pair_hash = _hash({
        "protocol_version": "manual-portfolio-pair-v1", "identity": identity(baseline),
        "baseline_manifest_sha256": baseline.manifest_file_sha256,
        "stress_manifest_sha256": stress.manifest_file_sha256,
    })
    baseline_final = verify_portfolio_evidence_directory(baseline.directory)
    stress_final = verify_portfolio_evidence_directory(stress.directory)
    if (
        baseline_final["manifest_file_sha256"] != baseline.manifest_file_sha256
        or stress_final["manifest_file_sha256"] != stress.manifest_file_sha256
    ):
        raise ValueError("portfolio_pair_changed_during_read")
    return {"pair_hash": pair_hash, "baseline": baseline, "stress": stress}
