"""Economic H2c holdout executor.

This module deliberately has no database dependency.  The caller supplies the
already frozen parent identities and the access lease facts; this service only
materialises a new, independently audited economic evaluation.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import replace
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable, Mapping

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from quant_engine.backtest.manual_portfolio_artifacts import (
    audit_portfolio_pair,
    artifact_code_hashes,
    controlled_path,
)
from quant_engine.backtest.manual_portfolio_evidence import _file_hash
from quant_engine.backtest.manual_portfolio_sources import _manifest_entry_file, _source_entry, load_verified_portfolio_sources
from quant_engine.backtest.manual_daily_portfolio_v3 import run_manual_daily_portfolio_v3
from quant_engine.factor.expression import FactorExpressionSpec
from quant_engine.factor.manual_daily_bundle import ManualDailyFactorBundleV2
from server.services.factor_research import _research_panel


PROTOCOL_VERSION = "manual-holdout-evaluation-v1"
TECHNICAL_EXIT_TAIL_DAYS = 20
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9._-]+$")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _sha_file(path: Path) -> str:
    return _file_hash(path)


def _safe(value: Any, field: str) -> str:
    text = str(value)
    if not text or not _SAFE_COMPONENT.fullmatch(text) or text in {".", ".."}:
        raise ValueError(f"holdout_{field}_invalid")
    return text


def _pulse(heartbeat: Callable[..., Any] | None, stage: str) -> None:
    if heartbeat is None:
        return
    heartbeat(stage)


def _controlled_existing(path: str | Path, root: Path, *, name: str, file: bool = True) -> Path:
    supplied = Path(path)
    if ".." in supplied.parts:
        raise ValueError(f"holdout_{name}_path_invalid")
    candidate = supplied if supplied.is_absolute() else root / supplied
    raw_cursor = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        raw_cursor /= part
        if raw_cursor.is_symlink():
            resolved_cursor = raw_cursor.resolve(strict=False)
            if raw_cursor.is_relative_to(root) or not root.is_relative_to(resolved_cursor):
                raise ValueError(f"holdout_{name}_path_invalid")
    try:
        try:
            resolved = controlled_path(candidate, root)
        except ValueError:
            # macOS /var is an alias for /private/var; preserve the existing
            # loader's alias behaviour after the lexical symlink check above.
            if not (candidate.is_absolute() and len(candidate.parts) > 1 and candidate.parts[1] == "var"):
                raise
            resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
        cursor = root
        for part in resolved.relative_to(root).parts:
            cursor /= part
            if cursor.is_symlink():
                raise ValueError("symlink")
        if file and not resolved.is_file():
            raise ValueError("not a file")
        if not file and not resolved.is_dir():
            raise ValueError("not a file")
        return resolved
    except (OSError, TypeError, ValueError) as exc:
        raise ValueError(f"holdout_{name}_path_invalid") from exc


def _controlled_destination(path: str | Path, root: Path, *, name: str) -> Path:
    """Validate a new destination without resolving away lexical symlinks."""
    supplied = Path(path)
    candidate = supplied if supplied.is_absolute() else root / supplied
    if ".." in candidate.parts:
        raise ValueError(f"holdout_{name}_path_invalid")
    cursor = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        cursor /= part
        if cursor.is_symlink():
            resolved_cursor = cursor.resolve(strict=False)
            # Permit only the platform /var -> /private/var alias.
            if cursor != Path("/var") or not str(resolved_cursor).startswith("/private/var"):
                raise ValueError(f"holdout_{name}_path_invalid")
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"holdout_{name}_path_invalid") from exc
    return resolved


def _load_json(path: Path, name: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"holdout_{name}_invalid") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"holdout_{name}_invalid")
    return payload


def _verify_dataset_manifest(path: Path, root: Path) -> dict[str, Any]:
    manifest = _load_json(path, "dataset_manifest")
    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) != {"daily", "actions", "securities", "calendar"}:
        raise ValueError("holdout_dataset_file_set_invalid")
    resolved: dict[str, Path] = {}
    for role in ("daily", "actions", "securities", "calendar"):
        item = files[role]
        if not isinstance(item, Mapping) or not isinstance(item.get("path"), str):
            raise ValueError(f"holdout_dataset_file_invalid:{role}")
        try:
            candidate = _manifest_entry_file(item["path"], path.parent, root)
        except (OSError, TypeError, ValueError) as exc:
            raise ValueError(f"holdout_dataset_file_invalid:{role}") from exc
        if item.get("sha256") != _sha_file(candidate):
            raise ValueError(f"holdout_dataset_file_hash_mismatch:{role}")
        resolved[role] = candidate
    identity = {
        "universe": manifest.get("universe"),
        "start_date": manifest.get("start_date"),
        "end_date": manifest.get("end_date"),
        "calendar_hash": (manifest.get("calendar") or {}).get("content_hash"),
        "files": {role: files[role].get("sha256") for role in files},
        "source_files": manifest.get("source_files"),
    }
    if manifest.get("content_hash") != _digest(identity):
        raise ValueError("holdout_dataset_content_hash_mismatch")
    manifest["_resolved_files"] = resolved
    return manifest


def _verify_benchmark_receipt(path: Path, root: Path, expected_id: str) -> tuple[dict[str, Any], Path]:
    receipt = _load_json(path, "benchmark_receipt")
    arguments = receipt.get("arguments")
    ticker = arguments.get("code") if isinstance(arguments, Mapping) else None
    if str(ticker) != str(expected_id):
        raise ValueError("holdout_benchmark_identity_mismatch")
    parquet = _controlled_existing(path.with_suffix(".parquet"), root, name="benchmark")
    if receipt.get("sha256") != _sha_file(parquet):
        raise ValueError("holdout_benchmark_source_hash_mismatch")
    return receipt, parquet


def _calendar_days(manifest: Mapping[str, Any], calendar_path: Path) -> tuple[date, ...]:
    frame = pd.read_parquet(calendar_path)
    if "date" not in frame.columns:
        raise ValueError("holdout_calendar_date_missing")
    days = tuple(pd.Timestamp(item).date() for item in frame["date"].tolist())
    if not days or len(set(days)) != len(days) or list(days) != sorted(days):
        raise ValueError("holdout_calendar_invalid")
    expected = str((manifest.get("calendar") or {}).get("content_hash"))
    if not expected or len(expected) != 64:
        raise ValueError("holdout_calendar_hash_missing")
    return days


def _window_days(days: tuple[date, ...], start: date, end: date) -> tuple[date, ...]:
    if start > end:
        raise ValueError("holdout_signal_window_invalid")
    selected = tuple(day for day in days if start <= day <= end)
    if not selected:
        raise ValueError("holdout_signal_window_calendar_empty")
    return selected


def _code_hashes() -> dict[str, str]:
    root = Path(__file__).resolve().parents[2]
    names = {
        "holdout_engine": root / "server/services/manual_holdout_engine.py",
        "factor_expression": root / "quant_engine/factor/expression.py",
        "factor_operators": root / "quant_engine/factor/operators.py",
        "factor_research": root / "server/services/factor_research.py",
    }
    names.update({f"portfolio:{key}": root / "quant_engine" / key for key in artifact_code_hashes()})
    return {key: _sha_file(path) for key, path in names.items()}


def get_holdout_engine_code_hashes() -> dict[str, str]:
    """Return the fixed code identity map used by DB freeze metadata."""
    return dict(_code_hashes())


def _write_signal_file(path: Path, rows: list[dict[str, Any]]) -> None:
    table = pa.Table.from_pandas(
        pd.DataFrame(rows, columns=["date", "code", "factor"]),
        preserve_index=False,
    )
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    if path.exists():
        raise FileExistsError("holdout_signal_output_exists")
    try:
        pq.write_table(table, temporary, compression="zstd")
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        parent_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _mark_failure(staging: Path, exc: BaseException) -> None:
    try:
        staging.mkdir(parents=True, exist_ok=True)
        (staging / "failure.json").write_text(
            json.dumps({"protocol_version": PROTOCOL_VERSION, "error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass


def _subset(expected: Mapping[str, Any], actual: Mapping[str, Any], prefix: str = "") -> None:
    for key, value in expected.items():
        if key not in actual:
            raise ValueError(f"holdout_context_missing:{prefix}{key}")
        if isinstance(value, Mapping):
            if not isinstance(actual[key], Mapping):
                raise ValueError(f"holdout_context_mismatch:{prefix}{key}")
            _subset(value, actual[key], f"{prefix}{key}.")
        elif actual[key] != value:
            raise ValueError(f"holdout_context_mismatch:{prefix}{key}")


def _run_impl(
    *,
    original_bundle: ManualDailyFactorBundleV2,
    expression: FactorExpressionSpec,
    dataset_manifest_path: str | Path,
    benchmark_receipt_path: str | Path,
    source_root: str | Path,
    output_dir: str | Path,
    evaluation_id: str,
    attempt: int,
    token: str,
    signal_start: date,
    signal_end: date,
    initial_capital: Decimal,
    benchmark_id: str,
    training_artifact_id: str,
    training_artifact_hash: str,
    validation_artifact_id: str,
    validation_artifact_hash: str,
    frozen_input: Mapping[str, Any],
    input_hash: str,
    access_id: str | None = None,
    heartbeat: Callable[..., Any] | None = None,
) -> Path:
    """Compute, audit and atomically publish one H2c baseline/stress pair."""
    if not isinstance(original_bundle, ManualDailyFactorBundleV2):
        raise TypeError("holdout_original_bundle_invalid")
    if not isinstance(expression, FactorExpressionSpec) or expression.role != "rank":
        raise ValueError("holdout_expression_role_invalid")
    if expression.expression_hash != original_bundle.factor_expression_hash:
        raise ValueError("holdout_expression_identity_mismatch")
    if original_bundle.training_evidence_hash != training_artifact_hash or original_bundle.validation_evidence_hash != validation_artifact_hash:
        raise ValueError("holdout_training_validation_identity_mismatch")
    if original_bundle.dataset_content_hash == "":
        raise ValueError("holdout_original_dataset_identity_missing")
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
        raise ValueError("holdout_attempt_invalid")
    evaluation_id, token = _safe(evaluation_id, "evaluation_id"), _safe(token, "token")
    if access_id is not None:
        access_id = _safe(access_id, "access_id")
    root = Path(source_root).resolve(strict=True)
    output = _controlled_destination(output_dir, root, name="output")
    manifest_path = _controlled_existing(dataset_manifest_path, root, name="dataset_manifest")
    receipt_path = _controlled_existing(benchmark_receipt_path, root, name="benchmark_receipt")
    final = output
    staging: Path | None = None
    if final.exists():
        raise FileExistsError("holdout_attempt_output_exists")
    final.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{final.name}.", dir=final.parent))
    try:
        _pulse(heartbeat, "verify_inputs")
        dataset = _verify_dataset_manifest(manifest_path, root)
        _verify_benchmark_receipt(receipt_path, root, benchmark_id)
        files = dataset["_resolved_files"]
        days = _calendar_days(dataset, files["calendar"])
        window = _window_days(days, signal_start, signal_end)
        execution_days = tuple(day for day in days if day >= window[0])
        required_end_index = days.index(window[-1]) + original_bundle.exit_offset + TECHNICAL_EXIT_TAIL_DAYS
        if required_end_index >= len(days):
            raise ValueError("holdout_execution_calendar_insufficient")
        execution_end = days[required_end_index]
        _pulse(heartbeat, "verify_inputs_done")

        _pulse(heartbeat, "compute_factor")
        panel_manifest = dict(dataset)
        panel_manifest["files"] = {
            role: {**dataset["files"][role], "path": str(dataset["_resolved_files"][role])}
            for role in ("daily", "actions", "securities", "calendar")
        }
        panel, _open_wide = _research_panel(manifest_path, panel_manifest, set(expression.required_fields), window[-1])
        if panel.index.duplicated().any():
            raise ValueError("holdout_factor_panel_duplicate")
        raw = expression.expression.evaluate(panel[list(expression.required_fields)])
        raw_numeric = pd.to_numeric(raw, errors="coerce")
        if np.isinf(np.asarray(raw_numeric, dtype=float)).any():
            raise ValueError("holdout_factor_infinity")
        factor = expression.compute(panel[list(expression.required_fields)]) * expression.direction
        rows: list[dict[str, Any]] = []
        for (code, day), value in factor.items():
            day = pd.Timestamp(day).date()
            if day < window[0] or day > window[-1] or pd.isna(value):
                continue
            if not np.isfinite(float(value)):
                raise ValueError("holdout_factor_infinity")
            rows.append({"date": day, "code": str(code).upper(), "factor": float(value)})
        rows.sort(key=lambda row: (row["date"], row["code"]))
        if len({(row["date"], row["code"]) for row in rows}) != len(rows):
            raise ValueError("holdout_signal_duplicate")
        input_dir = _controlled_destination(root / "holdout_inputs" / evaluation_id / f"attempt-{attempt}-{token}", root, name="signals")
        input_dir.parent.mkdir(parents=True, exist_ok=True)
        input_dir.mkdir(parents=True, exist_ok=False)
        signal_path = input_dir / "signals.parquet"
        _write_signal_file(signal_path, rows)
        signal_sha = _sha_file(signal_path)
        _pulse(heartbeat, "compute_factor_done")

        _pulse(heartbeat, "load_verified_sources")
        sources = load_verified_portfolio_sources(
            dataset_manifest_path=manifest_path,
            benchmark_receipt_path=receipt_path,
            signal_path=signal_path,
            signal_sha256=signal_sha,
            training_artifact_id=training_artifact_id,
            training_artifact_hash=training_artifact_hash,
            validation_artifact_id=validation_artifact_id,
            validation_artifact_hash=validation_artifact_hash,
            allowed_root=root,
        )
        if sources.input_manifest.benchmark_id != benchmark_id:
            raise ValueError("holdout_benchmark_identity_mismatch")
        if frozen_input["dataset"]["data_content_hash"] != sources.input_manifest.dataset_content_hash:
            raise ValueError("holdout_dataset_content_hash_mismatch")
        if not all(day in sources.calendar.days for day in window):
            raise ValueError("holdout_signal_calendar_mismatch")
        _pulse(heartbeat, "load_verified_sources_done")

        base_bundle = replace(original_bundle, dataset_content_hash=sources.input_manifest.dataset_content_hash, cost_scenario="baseline")
        stress_bundle = replace(base_bundle, cost_scenario="stress")
        results = {}
        for scenario, bundle in (("baseline", base_bundle), ("stress", stress_bundle)):
            _pulse(heartbeat, f"run_{scenario}")
            result = run_manual_daily_portfolio_v3(
                daily=sources.daily,
                eligibility=sources.eligibility,
                benchmark=sources.benchmark,
                signals=sources.signals,
                corporate_actions=sources.corporate_actions,
                calendar=sources.calendar,
                bundle=bundle,
                input_manifest=sources.input_manifest,
                start=window[0],
                end=execution_end,
                initial_capital=initial_capital,
            )
            if result.start_date != window[0] or result.end_date != execution_end or result.quality_errors:
                raise ValueError("holdout_quality_gate_failed")
            results[scenario] = result
            _pulse(heartbeat, f"run_{scenario}_done")

        _pulse(heartbeat, "write_pair")
        baseline_dir = results["baseline"].write_evidence(staging / "baseline", sources=sources)
        stress_dir = results["stress"].write_evidence(staging / "stress", sources=sources)
        pair = audit_portfolio_pair(baseline_dir, stress_dir, allowed_root=staging, source_root=root)
        for scenario in ("baseline", "stress"):
            scenario_manifest = _load_json(staging / scenario / "manifest.json", f"{scenario}_manifest")
            if scenario_manifest.get("generator_result_hash") != results[scenario].result_hash:
                raise ValueError("holdout_execution_protocol_mismatch")
        scenario_entries = {}
        for scenario, directory, audited in (("baseline", baseline_dir, pair["baseline"]), ("stress", stress_dir, pair["stress"])):
            manifest_file = directory / "manifest.json"
            scenario_entries[scenario] = {
                "path": directory.relative_to(staging).as_posix(),
                "manifest_sha256": _sha_file(manifest_file),
                "result_hash": audited.result.result_hash,
                "metrics": audited.metrics,
            }
        context = {
            "original_strategy_core_hash": original_bundle.strategy_core_hash,
            "original_bundle_hash": original_bundle.bundle_hash,
            "original_bundle": original_bundle.as_dict(),
            "derived_strategy_core_hash": base_bundle.strategy_core_hash,
            "derived_bundle": {"baseline": base_bundle.as_dict(), "stress": stress_bundle.as_dict()},
            "evaluation_core_hash": "",
            "access_id": access_id,
            "data_identity_replacement": {
                "field": "dataset_content_hash",
                "original": original_bundle.dataset_content_hash,
                "derived": sources.input_manifest.dataset_content_hash,
            },
            "signal_window": {"start": window[0].isoformat(), "end": window[-1].isoformat()},
            "requested_signal_window": {"start": signal_start.isoformat(), "end": signal_end.isoformat()},
            "execution_window": {"start": window[0].isoformat(), "end": execution_end.isoformat(), "exit_tail_policy": "technical-exit-tail-v1", "exit_tail_days": TECHNICAL_EXIT_TAIL_DAYS},
            "initial_capital": str(Decimal(str(initial_capital)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
            "benchmark_id": benchmark_id,
        }
        context["evaluation_core_hash"] = _digest({key: value for key, value in context.items() if key != "evaluation_core_hash"})
        expression_payload = expression.as_dict()
        for derived_key in ("expression_hash", "required_fields", "lookback"):
            expression_payload.pop(derived_key, None)
        receipt_map = {item["role"]: item for item in sources.source_locator()["receipts"]}
        top = {
            "protocol_version": PROTOCOL_VERSION,
            "evaluation_id": evaluation_id,
            "attempt": attempt,
            "token": token,
            "access_id": access_id,
            "frozen_input": dict(frozen_input),
            "input_hash": input_hash,
            "context": context,
            "expression": expression_payload,
            "expression_hash": expression.expression_hash,
            "direction": expression.direction,
            "input": {
                "dataset_manifest": {"path": receipt_map["dataset_manifest"]["path"], "sha256": receipt_map["dataset_manifest"]["sha256"]},
                "benchmark_receipt": {"path": receipt_map["benchmark_receipt"]["path"], "sha256": receipt_map["benchmark_receipt"]["sha256"]},
                "dataset_content_hash": sources.input_manifest.dataset_content_hash,
                "signal_path": next(item["path"] for item in sources.input_manifest.source_files if item["role"] == "signals"),
                "signal_sha256": signal_sha,
                "input_manifest_hash": sources.input_manifest.manifest_hash,
                "training_artifact_id": training_artifact_id,
                "training_artifact_hash": training_artifact_hash,
                "validation_artifact_id": validation_artifact_id,
                "validation_artifact_hash": validation_artifact_hash,
            },
            "scenarios": scenario_entries,
            "pair_hash": pair["pair_hash"],
            "metrics": {scenario: entry["metrics"] for scenario, entry in scenario_entries.items()},
            "quality_errors": sorted(set(results["baseline"].quality_errors + results["stress"].quality_errors)),
            "execution_protocol_reproduced": {"baseline": True, "stress": True},
            "code_hashes": _code_hashes(),
            "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        }
        top["self_hash"] = _digest(top)
        top_path = staging / "manifest.json"
        top_path.write_text(json.dumps(top, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        with top_path.open("rb") as handle:
            os.fsync(handle.fileno())
        staging_fd = os.open(staging, os.O_RDONLY)
        try:
            os.fsync(staging_fd)
        finally:
            os.close(staging_fd)
        _pulse(heartbeat, "write_pair_done")
        os.replace(staging, final)
        parent_fd = os.open(final.parent, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        return final / "manifest.json"
    except Exception as exc:
        _mark_failure(staging, exc)
        raise


def _nested_input(input_json: Mapping[str, Any], *, source_root: str | Path) -> dict[str, Any]:
    """Validate and flatten the persisted execution metadata input."""
    def section(name: str) -> Mapping[str, Any]:
        value = input_json.get(name)
        if not isinstance(value, Mapping):
            raise ValueError(f"holdout_input_section_missing:{name}")
        return value
    original = section("original")
    bundle_payload = original.get("bundle")
    factor = section("factor_expression")
    spec_payload = factor.get("spec")
    dataset = section("dataset")
    benchmark = section("benchmark")
    parent = section("parent")
    ids = {
        "training": parent.get("training_artifact_id"),
        "validation": parent.get("validation_artifact_id"),
    }
    hashes = {
        "training": parent.get("training_artifact_hash"),
        "validation": parent.get("validation_artifact_hash"),
    }
    signal = section("signal_window")
    technical = section("technical_policy")
    if not isinstance(bundle_payload, Mapping) or not isinstance(spec_payload, Mapping):
        raise ValueError("holdout_input_strategy_metadata_missing")
    if not isinstance(ids, Mapping) or not isinstance(hashes, Mapping):
        raise ValueError("holdout_input_parent_metadata_missing")
    if technical.get("technical_exit_tail_v1") != TECHNICAL_EXIT_TAIL_DAYS:
        raise ValueError("holdout_technical_policy_mismatch")
    required = {
        "manifest_path", "manifest_sha256", "files", "data_content_hash",
    }
    if required - set(dataset):
        raise ValueError("holdout_input_dataset_metadata_missing")
    if {"receipt_path", "receipt_sha256", "ticker"} - set(benchmark):
        raise ValueError("holdout_input_benchmark_metadata_missing")
    if {"start_date", "end_date"} - set(signal):
        raise ValueError("holdout_input_signal_window_missing")
    for key in ("training", "validation"):
        if key not in ids or key not in hashes:
            raise ValueError("holdout_input_parent_artifact_missing")
    if not isinstance(input_json.get("source_files"), (list, tuple)):
        raise ValueError("holdout_input_source_files_missing")
    if not isinstance(input_json.get("code_hashes"), Mapping):
        raise ValueError("holdout_input_code_hashes_missing")
    if any(input_json["code_hashes"].get(key) != value for key, value in _code_hashes().items()):
        raise ValueError("holdout_input_code_version_mismatch")
    source_root_path = Path(source_root).resolve(strict=True)
    manifest_path = _controlled_existing(dataset["manifest_path"], source_root_path, name="dataset_manifest")
    receipt_path = _controlled_existing(benchmark["receipt_path"], source_root_path, name="benchmark_receipt")
    if _sha_file(manifest_path) != dataset["manifest_sha256"]:
        raise ValueError("holdout_dataset_manifest_receipt_changed")
    if _sha_file(receipt_path) != benchmark["receipt_sha256"]:
        raise ValueError("holdout_benchmark_receipt_changed")
    manifest_payload = _load_json(manifest_path, "dataset_manifest")
    manifest_files = manifest_payload.get("files") or {}
    if dataset["data_content_hash"] != manifest_payload.get("content_hash"):
        raise ValueError("holdout_frozen_dataset_metadata_mismatch")
    for role, item in dataset["files"].items():
        if role not in manifest_files:
            raise ValueError("holdout_frozen_dataset_metadata_mismatch")
        try:
            resolved = _manifest_entry_file(manifest_files[role]["path"], manifest_path.parent, source_root_path)
            expected_entry = _source_entry(role, resolved, source_root_path)
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise ValueError("holdout_frozen_dataset_metadata_mismatch") from exc
        if dict(item) != expected_entry:
            raise ValueError("holdout_frozen_dataset_metadata_mismatch")
    receipt_payload = _load_json(receipt_path, "benchmark_receipt")
    receipt_ticker = (receipt_payload.get("arguments") or {}).get("code")
    if receipt_ticker != benchmark["ticker"]:
        raise ValueError("holdout_frozen_benchmark_metadata_mismatch")
    expression_payload = dict(spec_payload)
    for key in ("expression_hash", "required_fields", "lookback"):
        expression_payload.pop(key, None)
    expression = FactorExpressionSpec.from_dict(expression_payload)
    if factor.get("direction") != expression.direction:
        raise ValueError("holdout_expression_direction_mismatch")
    bundle = ManualDailyFactorBundleV2.from_dict(dict(bundle_payload))
    if bundle.factor_expression_hash != expression.expression_hash:
        raise ValueError("holdout_expression_parent_mismatch")
    return {
        "original_bundle": bundle,
        "expression": expression,
        "dataset_manifest_path": manifest_path,
        "benchmark_receipt_path": receipt_path,
        "signal_start": date.fromisoformat(str(signal.get("start_date") or signal["requested_start_date"])),
        "signal_end": date.fromisoformat(str(signal.get("end_date") or signal["requested_end_date"])),
        "initial_capital": Decimal(str(input_json["capital"])),
        "benchmark_id": str(benchmark["ticker"]),
        "training_artifact_id": str(ids["training"]),
        "training_artifact_hash": str(hashes["training"]),
        "validation_artifact_id": str(ids["validation"]),
        "validation_artifact_hash": str(hashes["validation"]),
        "frozen_input": dict(input_json),
        "input_hash": _digest(input_json),
    }


def run_holdout_engine(
    input_json: Mapping[str, Any], *, source_root: str | Path, output_dir: str | Path,
    evaluation_id: str, attempt: int, token: str, access_id: str,
    heartbeat: Callable[..., Any] | None = None,
) -> Path:
    """Public H2c interface; all strategy/economic identity comes from input_json."""
    if not isinstance(input_json, Mapping) or not access_id:
        raise ValueError("holdout_input_and_access_required")
    normalized = _nested_input(input_json, source_root=source_root)
    return _run_impl(
        **normalized, source_root=source_root, output_dir=output_dir,
        evaluation_id=evaluation_id, attempt=attempt, token=token,
        access_id=access_id, heartbeat=heartbeat,
    )


def verify_holdout_engine_manifest(
    path: str | Path,
    *,
    input_json: Mapping[str, Any],
    expected_access_id: str,
    source_root: str | Path,
    allowed_root: str | Path,
) -> dict[str, Any]:
    """Revalidate a published holdout manifest and independently replay both scenarios."""
    if not isinstance(input_json, Mapping) or not expected_access_id:
        raise ValueError("holdout_input_and_access_required")
    output = Path(allowed_root).resolve(strict=True)
    root = Path(source_root).resolve(strict=True)
    top_path = _controlled_existing(path, output, name="manifest")
    directory = top_path.parent
    top = _load_json(top_path, "holdout_manifest")
    if top.get("protocol_version") != PROTOCOL_VERSION:
        raise ValueError("holdout_manifest_protocol_invalid")
    if {item.name for item in directory.iterdir()} != {"manifest.json", "baseline", "stress"}:
        raise ValueError("holdout_manifest_file_set_mismatch")
    if any(not (directory / name).is_dir() for name in ("baseline", "stress")):
        raise ValueError("holdout_manifest_file_set_mismatch")
    if top.get("access_id") != expected_access_id:
        raise ValueError("holdout_access_identity_mismatch")
    expected_hash = top.get("self_hash")
    body = dict(top)
    body.pop("self_hash", None)
    if expected_hash != _digest(body):
        raise ValueError("holdout_manifest_hash_mismatch")
    if top.get("frozen_input") != dict(input_json) or top.get("input_hash") != _digest(input_json):
        raise ValueError("holdout_frozen_input_mismatch")
    frozen = dict(input_json)
    if any(frozen.get("code_hashes", {}).get(key) != value for key, value in _code_hashes().items()):
        raise ValueError("holdout_input_code_version_mismatch")
    if top.get("code_hashes") != _code_hashes():
        raise ValueError("holdout_engine_code_version_mismatch")
    expression = FactorExpressionSpec.from_dict(top["expression"])
    if expression.expression_hash != top.get("expression_hash") or expression.direction != top.get("direction") or expression.role != "rank":
        raise ValueError("holdout_expression_hash_mismatch")
    context = top.get("context")
    if not isinstance(context, Mapping):
        raise ValueError("holdout_context_invalid")
    if context.get("evaluation_core_hash") != _digest({key: value for key, value in context.items() if key != "evaluation_core_hash"}):
        raise ValueError("holdout_evaluation_core_hash_mismatch")
    if top.get("access_id") != context.get("access_id"):
        raise ValueError("holdout_access_identity_mismatch")
    original = ManualDailyFactorBundleV2.from_dict(context["original_bundle"])
    if original.factor_expression_hash != expression.expression_hash:
        raise ValueError("holdout_expression_parent_mismatch")
    spec_payload = dict(frozen["factor_expression"]["spec"])
    for key in ("expression_hash", "required_fields", "lookback"):
        spec_payload.pop(key, None)
    frozen_expression = FactorExpressionSpec.from_dict(spec_payload)
    if frozen_expression.expression_hash != expression.expression_hash or frozen["factor_expression"]["direction"] != expression.direction:
        raise ValueError("holdout_frozen_expression_mismatch")
    frozen_bundle = ManualDailyFactorBundleV2.from_dict(dict(frozen["original"]["bundle"]))
    if frozen_bundle.as_dict() != original.as_dict():
        raise ValueError("holdout_frozen_bundle_mismatch")
    derived = context["derived_bundle"]
    baseline_expected = replace(original, dataset_content_hash=derived["baseline"]["dataset_content_hash"], cost_scenario="baseline").as_dict()
    stress_expected = replace(original, dataset_content_hash=derived["stress"]["dataset_content_hash"], cost_scenario="stress").as_dict()
    if derived["baseline"] != baseline_expected:
        raise ValueError("holdout_derived_bundle_invalid")
    if derived["stress"] != stress_expected:
        raise ValueError("holdout_derived_bundle_invalid")
    if context["original_strategy_core_hash"] != original.strategy_core_hash or context["original_bundle_hash"] != original.bundle_hash:
        raise ValueError("holdout_original_bundle_identity_mismatch")
    if context.get("derived_strategy_core_hash") != baseline_expected["strategy_core_hash"]:
        raise ValueError("holdout_derived_core_hash_mismatch")
    dataset_path = _controlled_existing(frozen["dataset"]["manifest_path"], root, name="dataset_manifest")
    dataset = _verify_dataset_manifest(dataset_path, root)
    days = _calendar_days(dataset, dataset["_resolved_files"]["calendar"])
    frozen_start = date.fromisoformat(str(frozen["signal_window"].get("start_date")))
    frozen_end = date.fromisoformat(str(frozen["signal_window"].get("end_date")))
    signal_days = _window_days(days, frozen_start, frozen_end)
    tail_index = days.index(signal_days[-1]) + original.exit_offset + TECHNICAL_EXIT_TAIL_DAYS
    if tail_index >= len(days) or context["execution_window"]["end"] != days[tail_index].isoformat():
        raise ValueError("holdout_execution_calendar_mismatch")
    if context["signal_window"] != {"start": signal_days[0].isoformat(), "end": signal_days[-1].isoformat()}:
        raise ValueError("holdout_signal_calendar_mismatch")
    panel_manifest = dict(dataset)
    panel_manifest["files"] = {
        role: {**dataset["files"][role], "path": str(dataset["_resolved_files"][role])}
        for role in ("daily", "actions", "securities", "calendar")
    }
    panel, _ = _research_panel(dataset_path, panel_manifest, set(expression.required_fields), signal_days[-1])
    expected_factor = expression.compute(panel[list(expression.required_fields)]) * expression.direction
    expected_rows = [
        {"date": pd.Timestamp(day).date(), "code": str(code).upper(), "factor": float(value)}
        for (code, day), value in expected_factor.items()
        if signal_days[0] <= pd.Timestamp(day).date() <= signal_days[-1] and not pd.isna(value)
    ]
    signal_path = _controlled_existing(top["input"]["signal_path"], root, name="signals")
    if _sha_file(signal_path) != top["input"].get("signal_sha256"):
        raise ValueError("holdout_signal_source_hash_mismatch")
    signal_frame = pd.read_parquet(signal_path)
    if set(("date", "code", "factor")) - set(signal_frame.columns) or signal_frame.duplicated(["date", "code"]).any():
        raise ValueError("holdout_signal_source_invalid")
    actual_rows = [
        {"date": pd.Timestamp(row["date"]).date(), "code": str(row["code"]).upper(), "factor": float(row["factor"])}
        for row in signal_frame[["date", "code", "factor"]].to_dict("records")
    ]
    expected_rows.sort(key=lambda item: (item["date"], item["code"]))
    actual_rows.sort(key=lambda item: (item["date"], item["code"]))
    if len(actual_rows) != len(expected_rows) or any(
        left["date"] != right["date"] or left["code"] != right["code"] or not np.isclose(left["factor"], right["factor"], rtol=0, atol=1e-12)
        for left, right in zip(actual_rows, expected_rows)
    ):
        raise ValueError("holdout_signal_expression_mismatch")
    for scenario in ("baseline", "stress"):
        entry = top.get("scenarios", {}).get(scenario)
        if not isinstance(entry, Mapping):
            raise ValueError("holdout_scenario_manifest_missing")
        scenario_dir = _controlled_existing(directory / str(entry["path"]), directory, name="scenario", file=False)
        manifest_file = scenario_dir / "manifest.json"
        if _sha_file(manifest_file) != entry.get("manifest_sha256"):
            raise ValueError("holdout_scenario_manifest_hash_mismatch")
    pair = audit_portfolio_pair(
        directory / top["scenarios"]["baseline"]["path"],
        directory / top["scenarios"]["stress"]["path"],
        allowed_root=directory,
        source_root=root,
    )
    if pair["pair_hash"] != top.get("pair_hash"):
        raise ValueError("holdout_pair_hash_mismatch")
    reproduced: dict[str, bool] = {}
    for scenario in ("baseline", "stress"):
        scenario_dir = directory / top["scenarios"][scenario]["path"]
        scenario_manifest = _load_json(scenario_dir / "manifest.json", f"{scenario}_manifest")
        result = pair[scenario].result
        locator = scenario_manifest.get("source_locator") or {}
        receipts = {item.get("role"): item for item in locator.get("receipts", [])}
        dataset_receipt = _controlled_existing(receipts["dataset_manifest"]["path"], root, name="dataset_manifest")
        benchmark_receipt = _controlled_existing(receipts["benchmark_receipt"]["path"], root, name="benchmark_receipt")
        signal_entry = next(item for item in result.input_manifest.source_files if item.get("role") == "signals")
        signal_file = _controlled_existing(signal_entry["path"], root, name="signals")
        replay_sources = load_verified_portfolio_sources(
            dataset_manifest_path=dataset_receipt,
            benchmark_receipt_path=benchmark_receipt,
            signal_path=signal_file,
            signal_sha256=result.input_manifest.signal_content_hash,
            training_artifact_id=result.input_manifest.training_artifact_id,
            training_artifact_hash=result.input_manifest.training_artifact_hash,
            validation_artifact_id=result.input_manifest.validation_artifact_id,
            validation_artifact_hash=result.input_manifest.validation_artifact_hash,
            allowed_root=root,
        )
        reproduced_result = run_manual_daily_portfolio_v3(
            daily=replay_sources.daily, eligibility=replay_sources.eligibility,
            benchmark=replay_sources.benchmark, signals=replay_sources.signals,
            corporate_actions=replay_sources.corporate_actions, calendar=replay_sources.calendar,
            bundle=result.bundle, input_manifest=replay_sources.input_manifest,
            start=result.start_date, end=result.end_date, initial_capital=result.initial_capital,
        )
        from quant_engine.backtest.manual_portfolio_artifacts import reconstruct_result, run_envelope
        from quant_engine.backtest.manual_portfolio_evidence import _ARROW_SCHEMAS, _table
        rows_by_file = {
            "signals.parquet": reproduced_result.signals,
            "order_intents.parquet": reproduced_result.intents,
            "order_attempts.parquet": reproduced_result.attempts,
            "trades.parquet": reproduced_result.trades,
            "corporate_actions.parquet": reproduced_result.corporate_actions,
            "positions.parquet": reproduced_result.positions,
            "daily_portfolio.parquet": reproduced_result.daily,
            "benchmark.parquet": reproduced_result.benchmark,
        }
        fresh_audit = {
            "schema_version": "manual-portfolio-audit-v1",
            "items": reproduced_result.audit,
            "quality_errors": reproduced_result.quality_errors,
            "error_count": sum(item.get("severity") == "error" for item in reproduced_result.audit),
            "warning_count": sum(item.get("severity") == "warning" for item in reproduced_result.audit),
        }
        normalized = reconstruct_result(
            run_envelope(reproduced_result), reproduced_result.input_manifest.__dict__,
            {name: _table(rows_by_file[name], schema) for name, schema in _ARROW_SCHEMAS.items()},
            fresh_audit,
        )
        reproduced[scenario] = (
            reproduced_result.result_hash == scenario_manifest.get("generator_result_hash")
            and normalized.result_hash == result.result_hash
        )
        if not reproduced[scenario]:
            raise ValueError("holdout_execution_protocol_mismatch")
    if top.get("execution_protocol_reproduced") != reproduced:
        raise ValueError("holdout_execution_protocol_mismatch")
    expected_bundles = {"baseline": baseline_expected, "stress": stress_expected}
    signal_start = context["signal_window"]["start"]
    execution_end = context["execution_window"]["end"]
    input_payload = top.get("input")
    if not isinstance(input_payload, Mapping):
        raise ValueError("holdout_input_invalid")
    if input_payload.get("dataset_content_hash") != frozen["dataset"]["data_content_hash"]:
        raise ValueError("holdout_frozen_dataset_mismatch")
    if input_payload.get("benchmark_receipt", {}).get("sha256") != frozen["benchmark"]["receipt_sha256"]:
        raise ValueError("holdout_frozen_benchmark_mismatch")
    if input_payload.get("dataset_manifest", {}).get("sha256") != frozen["dataset"]["manifest_sha256"]:
        raise ValueError("holdout_frozen_dataset_manifest_mismatch")
    if context.get("benchmark_id") != frozen["benchmark"]["ticker"]:
        raise ValueError("holdout_frozen_benchmark_mismatch")
    if Decimal(str(context.get("initial_capital"))) != Decimal(str(frozen["capital"])):
        raise ValueError("holdout_frozen_capital_mismatch")
    if context.get("requested_signal_window", {}).get("start") != str(frozen["signal_window"]["start_date"]):
        raise ValueError("holdout_frozen_signal_window_mismatch")
    if context.get("requested_signal_window", {}).get("end") != str(frozen["signal_window"]["end_date"]):
        raise ValueError("holdout_frozen_signal_window_mismatch")
    for scenario in ("baseline", "stress"):
        audited = pair[scenario]
        entry = top["scenarios"][scenario]
        result = audited.result
        if result.bundle.as_dict() != expected_bundles[scenario]:
            raise ValueError("holdout_scenario_bundle_mismatch")
        if result.input_manifest.dataset_content_hash != input_payload.get("dataset_content_hash"):
            raise ValueError("holdout_dataset_identity_mismatch")
        if result.input_manifest.signal_content_hash != input_payload.get("signal_sha256"):
            raise ValueError("holdout_signal_identity_mismatch")
        if result.input_manifest.training_artifact_id != input_payload.get("training_artifact_id") or result.input_manifest.training_artifact_hash != input_payload.get("training_artifact_hash"):
            raise ValueError("holdout_training_identity_mismatch")
        if result.input_manifest.validation_artifact_id != input_payload.get("validation_artifact_id") or result.input_manifest.validation_artifact_hash != input_payload.get("validation_artifact_hash"):
            raise ValueError("holdout_validation_identity_mismatch")
        if result.input_manifest.benchmark_id != context.get("benchmark_id"):
            raise ValueError("holdout_benchmark_identity_mismatch")
        if result.input_manifest.training_artifact_id != frozen["parent"]["training_artifact_id"] or result.input_manifest.training_artifact_hash != frozen["parent"]["training_artifact_hash"]:
            raise ValueError("holdout_frozen_training_identity_mismatch")
        if result.input_manifest.validation_artifact_id != frozen["parent"]["validation_artifact_id"] or result.input_manifest.validation_artifact_hash != frozen["parent"]["validation_artifact_hash"]:
            raise ValueError("holdout_frozen_validation_identity_mismatch")
        if result.start_date.isoformat() != signal_start or result.end_date.isoformat() != execution_end:
            raise ValueError("holdout_window_identity_mismatch")
        if Decimal(str(result.initial_capital)) != Decimal(str(context.get("initial_capital"))):
            raise ValueError("holdout_capital_identity_mismatch")
        if audited.result.result_hash != entry["result_hash"] or audited.metrics != entry["metrics"] or audited.metrics != top["metrics"][scenario]:
            raise ValueError("holdout_scenario_summary_mismatch")
    actual_quality_errors = sorted(set(pair["baseline"].result.quality_errors + pair["stress"].result.quality_errors))
    if top.get("quality_errors") != actual_quality_errors:
        raise ValueError("holdout_quality_errors_mismatch")
    return {
        "manifest": top,
        "manifest_file_sha256": _sha_file(top_path),
        "pair_hash": pair["pair_hash"],
        "metrics": top["metrics"],
        "original_strategy_core_hash": context["original_strategy_core_hash"],
        "derived_strategy_core_hash": context["derived_strategy_core_hash"],
        "evaluation_core_hash": context["evaluation_core_hash"],
        "independent_replays": {
            "baseline": pair["baseline"].replay,
            "stress": pair["stress"].replay,
        },
        "quality_errors": sorted(set(
            pair["baseline"].result.quality_errors + pair["stress"].result.quality_errors
        )),
        "files": {
            scenario: {
                "path": top["scenarios"][scenario]["path"],
                "manifest_sha256": top["scenarios"][scenario]["manifest_sha256"],
            }
            for scenario in ("baseline", "stress")
        },
        "baseline": pair["baseline"],
        "stress": pair["stress"],
    }


__all__ = [
    "run_holdout_engine", "verify_holdout_engine_manifest",
    "get_holdout_engine_code_hashes", "TECHNICAL_EXIT_TAIL_DAYS",
]
