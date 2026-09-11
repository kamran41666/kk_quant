"""Engineering tests for the database-free H2c economic executor."""
from __future__ import annotations

import json
import copy
import shutil
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from quant_engine.factor.expression import FactorExpressionSpec
from quant_engine.factor.manual_daily_bundle import ManualDailyFactorBundleV2
from quant_engine.backtest.manual_portfolio_artifacts import audit_portfolio_pair, read_portfolio_artifact
from quant_engine.backtest.manual_portfolio_sources import load_verified_portfolio_sources
from quant_engine.backtest.manual_portfolio_evidence import _hash as evidence_hash
from server.services.manual_holdout_engine import (
    get_holdout_engine_code_hashes,
    run_holdout_engine,
    verify_holdout_engine_manifest,
)
from tests.test_manual_portfolio_sources import _sources


def _case(tmp_path: Path, *, days: int = 30):
    manifest_path, receipt_path, _ = _sources(tmp_path, day_count=days, unexplained=False)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expression = FactorExpressionSpec.from_dict({
        "name": "holdout_factor",
        "hypothesis": "fixture",
        "direction": 1,
        "role": "rank",
        "expression": {"field": "close"},
    })
    bundle = ManualDailyFactorBundleV2(
        strategy_key="holdout-test", factor_name="parent-display-name",
        factor_expression_hash=expression.expression_hash,
        label_spec_hash="2" * 64, training_evidence_hash="3" * 64,
        validation_evidence_hash="4" * 64, dataset_content_hash="5" * 64,
        top_n=1,
    )
    return manifest_path, receipt_path, manifest, expression, bundle


def _run(tmp_path: Path, *, days: int = 30):
    manifest_path, receipt_path, manifest, expression, bundle = _case(tmp_path, days=days)
    output = tmp_path / "results"
    input_json = _input(manifest_path, receipt_path, manifest, expression, bundle)
    path = run_holdout_engine(input_json, source_root=tmp_path, output_dir=output,
                              evaluation_id="eval", attempt=1, token="attempt-token",
                              access_id="access-1")
    return path, bundle, output, input_json


def _input(manifest_path, receipt_path, manifest, expression, bundle):
    spec = expression.as_dict()
    for key in ("expression_hash", "required_fields", "lookback"):
        spec.pop(key, None)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    source_root = manifest_path.parent.parent
    files = {
        role: {"role": role, "path": Path(item["path"]).resolve().relative_to(source_root.resolve()).as_posix(),
               "size": Path(item["path"]).stat().st_size, "sha256": item["sha256"]}
        for role, item in manifest["files"].items()
    }
    return {
        "original": {"bundle": bundle.as_dict()},
        "factor_expression": {"spec": spec, "direction": expression.direction},
        "dataset": {"manifest_path": str(manifest_path), "manifest_sha256": _sha(manifest_path),
                     "files": files, "data_content_hash": manifest["content_hash"]},
        "benchmark": {"receipt_path": str(receipt_path), "receipt_sha256": _sha(receipt_path),
                       "ticker": receipt["arguments"]["code"]},
        "parent": {"training_artifact_id": "training", "training_artifact_hash": "3" * 64,
                   "validation_artifact_id": "validation", "validation_artifact_hash": "4" * 64},
        "capital": "100000",
        "signal_window": {"start_date": manifest["start_date"],
                           "end_date": (date.fromisoformat(manifest["start_date"]) + timedelta(days=2)).isoformat()},
        "technical_policy": {"technical_exit_tail_v1": 20},
        "source_files": [],
        "code_hashes": get_holdout_engine_code_hashes(),
    }


def _sha(path):
    import hashlib
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def test_holdout_writes_two_audited_scenarios_and_preserves_parent(tmp_path):
    path, original, output, input_json = _run(tmp_path)
    top = json.loads(path.read_text(encoding="utf-8"))
    assert top["context"]["original_bundle_hash"] == original.bundle_hash
    assert top["context"]["original_bundle"]["dataset_content_hash"] == "5" * 64
    assert top["context"]["derived_bundle"]["baseline"]["dataset_content_hash"] != original.dataset_content_hash
    assert top["scenarios"].keys() == {"baseline", "stress"}
    audited = verify_holdout_engine_manifest(path, input_json=input_json, source_root=tmp_path,
                                             allowed_root=output, expected_access_id="access-1")
    assert set(audited["metrics"]) == {"baseline", "stress"}
    assert set(audited["independent_replays"]) == {"baseline", "stress"}
    assert audited["quality_errors"] == []
    assert audited["derived_strategy_core_hash"] == top["context"]["derived_strategy_core_hash"]


def test_holdout_rejects_calendar_shortfall_and_benchmark_mismatch(tmp_path):
    manifest_path, receipt_path, manifest, expression, bundle = _case(tmp_path, days=20)
    output = tmp_path / "results"
    kwargs = _input(manifest_path, receipt_path, manifest, expression, bundle)
    kwargs["signal_window"]["end_date"] = kwargs["signal_window"]["start_date"]
    with pytest.raises(ValueError, match="calendar_insufficient"):
        run_holdout_engine(kwargs, source_root=tmp_path, output_dir=output, evaluation_id="eval", attempt=1, token="token", access_id="a")
    kwargs = copy.deepcopy(kwargs)
    kwargs["benchmark"]["ticker"] = "sh.000905"
    with pytest.raises(ValueError, match="benchmark_(identity|metadata)"):
        run_holdout_engine(kwargs, source_root=tmp_path, output_dir=output, evaluation_id="eval2", attempt=1, token="token", access_id="a")


def test_holdout_rejects_tampered_top_and_scenario_manifest(tmp_path):
    path, _bundle, output, input_json = _run(tmp_path)
    top = json.loads(path.read_text(encoding="utf-8"))
    top["metrics"]["baseline"]["total_return"] = "999"
    path.write_text(json.dumps(top), encoding="utf-8")
    with pytest.raises(ValueError, match="manifest_hash"):
        verify_holdout_engine_manifest(path, input_json=input_json, source_root=tmp_path, allowed_root=output, expected_access_id="access-1")


def test_holdout_rejects_replaced_frozen_dataset_capital_and_expression(tmp_path):
    path, _bundle, output, input_json = _run(tmp_path)
    for section, key, value in (
        ("dataset", "data_content_hash", "f" * 64),
        ("factor_expression", "direction", -1),
        (None, "capital", "999999"),
    ):
        changed = copy.deepcopy(input_json)
        (changed[section] if section else changed)[key] = value
        with pytest.raises(ValueError, match="frozen_input|expression|content_hash"):
            verify_holdout_engine_manifest(path, input_json=changed, source_root=tmp_path,
                                           allowed_root=output, expected_access_id="access-1")


def test_holdout_rejects_internal_symlink_and_output_escape(tmp_path):
    manifest_path, receipt_path, manifest, expression, bundle = _case(tmp_path)
    frozen = _input(manifest_path, receipt_path, manifest, expression, bundle)
    alias = tmp_path / "root-link"
    alias.symlink_to(tmp_path, target_is_directory=True)
    linked = copy.deepcopy(frozen)
    linked["dataset"]["manifest_path"] = str(alias / "dataset" / "manifest.json")
    with pytest.raises(ValueError, match="path_invalid"):
        run_holdout_engine(linked, source_root=tmp_path, output_dir=tmp_path / "results",
                           evaluation_id="eval", attempt=1, token="token", access_id="a")
    outside = tmp_path.parent / "holdout-outside"
    with pytest.raises(ValueError, match="path_invalid"):
        run_holdout_engine(frozen, source_root=tmp_path, output_dir=outside,
                           evaluation_id="eval", attempt=1, token="token", access_id="a")


def test_holdout_rejects_persisted_budget_tamper_with_old_generator_hash(tmp_path):
    path, _bundle, output, input_json = _run(tmp_path)
    root = path.parent
    baseline_dir = root / "baseline"
    baseline = read_portfolio_artifact(baseline_dir, allowed_root=root, source_root=tmp_path)
    locator = baseline.manifest["source_locator"]
    receipts = {item["role"]: tmp_path / item["path"] for item in locator["receipts"]}
    signal_item = next(item for item in baseline.result.input_manifest.source_files if item["role"] == "signals")
    sources = load_verified_portfolio_sources(
        dataset_manifest_path=receipts["dataset_manifest"],
        benchmark_receipt_path=receipts["benchmark_receipt"],
        signal_path=tmp_path / signal_item["path"], signal_sha256=baseline.result.input_manifest.signal_content_hash,
        training_artifact_id=baseline.result.input_manifest.training_artifact_id,
        training_artifact_hash=baseline.result.input_manifest.training_artifact_hash,
        validation_artifact_id=baseline.result.input_manifest.validation_artifact_id,
        validation_artifact_hash=baseline.result.input_manifest.validation_artifact_hash,
        allowed_root=tmp_path,
    )
    tampered = baseline.result
    tampered.cohorts[0]["budget"] = str(Decimal(tampered.cohorts[0]["budget"]) + Decimal("1.00"))
    for signal_row in tampered.signals:
        if signal_row.get("cohort_id") == tampered.cohorts[0]["id"]:
            signal_row["cohort_budget"] = tampered.cohorts[0]["budget"]
    replacement = tampered.write_evidence(tmp_path / "tampered-baseline", sources=sources)
    old_generator_hash = baseline.manifest["generator_result_hash"]
    replacement_summary = json.loads((replacement / "summary.json").read_text(encoding="utf-8"))
    replacement_summary["generator_result_hash"] = old_generator_hash
    (replacement / "summary.json").write_text(json.dumps(replacement_summary), encoding="utf-8")
    replacement_manifest = json.loads((replacement / "manifest.json").read_text(encoding="utf-8"))
    for entry in replacement_manifest["files"]:
        if entry["path"] == "summary.json":
            summary_path = replacement / "summary.json"
            entry["size"] = summary_path.stat().st_size
            entry["sha256"] = _sha(summary_path)
            entry["logical_content_hash"] = evidence_hash(replacement_summary)
    replacement_manifest["generator_result_hash"] = old_generator_hash
    replacement_manifest["manifest_hash"] = evidence_hash({key: value for key, value in replacement_manifest.items() if key != "manifest_hash"})
    (replacement / "manifest.json").write_text(json.dumps(replacement_manifest), encoding="utf-8")
    shutil.rmtree(baseline_dir)
    shutil.move(str(replacement), str(baseline_dir))
    top = json.loads(path.read_text(encoding="utf-8"))
    audited_pair = audit_portfolio_pair(baseline_dir, root / "stress", allowed_root=root, source_root=tmp_path)
    top["scenarios"]["baseline"]["manifest_sha256"] = _sha(baseline_dir / "manifest.json")
    top["scenarios"]["baseline"]["result_hash"] = audited_pair["baseline"].result.result_hash
    top["scenarios"]["baseline"]["metrics"] = audited_pair["baseline"].metrics
    top["metrics"]["baseline"] = audited_pair["baseline"].metrics
    top["pair_hash"] = audited_pair["pair_hash"]
    top["self_hash"] = evidence_hash({key: value for key, value in top.items() if key != "self_hash"})
    path.write_text(json.dumps(top), encoding="utf-8")
    with pytest.raises(ValueError, match="execution_protocol_mismatch"):
        verify_holdout_engine_manifest(path, input_json=input_json, source_root=tmp_path,
                                       allowed_root=output, expected_access_id="access-1")
