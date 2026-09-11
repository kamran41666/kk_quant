import json
import shutil
from dataclasses import replace
from decimal import Decimal

import pytest
import pyarrow as pa
import pyarrow.parquet as pq

from quant_engine.backtest.manual_daily_portfolio_v3 import (
    run_manual_daily_portfolio_v3,
)
from quant_engine.backtest.manual_portfolio_artifacts import (
    audit_portfolio_pair,
    read_portfolio_artifact,
)
from quant_engine.backtest.manual_portfolio_evidence import (
    _parquet_entry,
    _hash,
    _json_entry,
    verify_portfolio_evidence_directory,
)
from quant_engine.backtest.manual_portfolio_sources import (
    _controlled_file,
    load_verified_portfolio_sources,
)
from tests.test_manual_daily_factor_research import _bundle
from tests.test_manual_portfolio_sources import _file_hash, _sources


def _build(tmp_path, *, scenario="baseline", with_action=False, capital=100_000):
    tmp_path.mkdir(parents=True, exist_ok=True)
    manifest_path, receipt_path, signal_path = _sources(
        tmp_path, day_count=6, unexplained=False, with_action=with_action,
    )
    sources = load_verified_portfolio_sources(
        dataset_manifest_path=manifest_path,
        benchmark_receipt_path=receipt_path,
        signal_path=signal_path,
        signal_sha256=_file_hash(signal_path),
        training_artifact_id="training",
        training_artifact_hash="1" * 64,
        validation_artifact_id="validation",
        validation_artifact_hash="2" * 64,
        allowed_root=tmp_path,
    )
    bundle = replace(
        _bundle(),
        dataset_content_hash=sources.input_manifest.dataset_content_hash,
        training_evidence_hash="1" * 64,
        validation_evidence_hash="2" * 64,
        cost_scenario=scenario,
    )
    result = run_manual_daily_portfolio_v3(
        daily=sources.daily,
        eligibility=sources.eligibility,
        benchmark=sources.benchmark,
        signals=sources.signals,
        corporate_actions=sources.corporate_actions,
        calendar=sources.calendar,
        bundle=bundle,
        input_manifest=sources.input_manifest,
        start=sources.calendar.days[0],
        end=sources.calendar.days[-1],
        initial_capital=capital,
    )
    return result, sources


def _rerun(result, sources, *, scenario, capital=None):
    return run_manual_daily_portfolio_v3(
        daily=sources.daily,
        eligibility=sources.eligibility,
        benchmark=sources.benchmark,
        signals=sources.signals,
        corporate_actions=sources.corporate_actions,
        calendar=sources.calendar,
        bundle=replace(result.bundle, cost_scenario=scenario),
        input_manifest=sources.input_manifest,
        start=result.start_date,
        end=result.end_date,
        initial_capital=result.initial_capital if capital is None else capital,
    )


def test_artifact_roundtrip_and_pair_reconstruct_from_disk(tmp_path):
    baseline, sources = _build(tmp_path / "baseline-source")
    stress = _rerun(baseline, sources, scenario="stress")
    baseline_dir = baseline.write_evidence(tmp_path / "baseline", sources=sources)
    stress_dir = stress.write_evidence(tmp_path / "stress", sources=sources)
    baseline_read = read_portfolio_artifact(
        baseline_dir, allowed_root=tmp_path, source_root=tmp_path / "baseline-source",
    )
    stress_read = read_portfolio_artifact(
        stress_dir, allowed_root=tmp_path, source_root=tmp_path / "baseline-source",
    )
    assert baseline_read.manifest["protocol_version"] == "manual-daily-portfolio-evidence-v2"
    assert baseline_read.manifest["result_hash"] == baseline_read.result.result_hash
    assert baseline_read.manifest["result_hash"] != baseline_read.manifest["generator_result_hash"]
    assert baseline_read.replay["passed"] and stress_read.replay["passed"]
    pair = audit_portfolio_pair(
        baseline_dir, stress_dir, allowed_root=tmp_path, source_root=tmp_path / "baseline-source",
    )
    assert pair["baseline"].result.bundle.cost_scenario == "baseline"
    assert pair["stress"].result.bundle.cost_scenario == "stress"

    migrated = tmp_path / "migrated"
    shutil.copytree(tmp_path / "baseline-source", migrated / "source")
    shutil.copytree(baseline_dir, migrated / "artifact")
    shutil.rmtree(tmp_path / "baseline-source")
    migrated_read = read_portfolio_artifact(
        migrated / "artifact", allowed_root=migrated, source_root=migrated / "source",
    )
    assert migrated_read.replay["passed"]


def test_loader_accepts_relative_api_paths_under_allowed_root(tmp_path):
    source_root = tmp_path / "source"
    _result, sources = _build(source_root)
    loaded = load_verified_portfolio_sources(
        dataset_manifest_path="dataset/manifest.json",
        benchmark_receipt_path="benchmark.json",
        signal_path="signals.parquet",
        signal_sha256=sources.input_manifest.signal_content_hash,
        training_artifact_id="training",
        training_artifact_hash="1" * 64,
        validation_artifact_id="validation",
        validation_artifact_hash="2" * 64,
        allowed_root=source_root,
    )
    assert loaded.verify_files()


def test_source_path_rejects_symlink_pointing_back_to_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "link").symlink_to(root, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        _controlled_file("link/anything.parquet", root, root)


def test_reader_rejects_tampered_receipt_and_parent_symlink(tmp_path):
    result, sources = _build(tmp_path / "source")
    output = result.write_evidence(tmp_path / "artifact", sources=sources)
    source_manifest = tmp_path / "source" / "dataset" / "manifest.json"
    payload = json.loads(source_manifest.read_text())
    payload["quality"] = [{"unexplained_reference_adjustments": ["tampered"]}]
    source_manifest.write_text(json.dumps(payload), encoding="utf-8")
    assert sources.verify_files() is False
    with pytest.raises(ValueError, match="portfolio_source_receipt_changed"):
        read_portfolio_artifact(output, allowed_root=tmp_path, source_root=tmp_path / "source")

    result, sources = _build(tmp_path / "source-2")
    output = result.write_evidence(tmp_path / "artifact-2", sources=sources)
    (tmp_path / "artifact-link").symlink_to(output, target_is_directory=True)
    with pytest.raises(ValueError, match="portfolio_artifact_symlink"):
        read_portfolio_artifact(
            tmp_path / "artifact-link", allowed_root=tmp_path, source_root=tmp_path / "source-2",
        )


def test_reader_rejects_persisted_duplicate_signal_primary_key(tmp_path):
    result, sources = _build(tmp_path / "source")
    output = result.write_evidence(tmp_path / "artifact", sources=sources)
    path = output / "signals.parquet"
    table = pq.read_table(path)
    duplicated = pa.concat_tables([table, table.slice(0, 1)])
    pq.write_table(duplicated, path, compression="zstd")
    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    entry = next(item for item in manifest["files"] if item["path"] == "signals.parquet")
    entry.update(_parquet_entry(path, "signals", duplicated))
    manifest["manifest_hash"] = _hash({key: value for key, value in manifest.items() if key != "manifest_hash"})
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    with pytest.raises(ValueError, match="primary_key_mismatch:signals.parquet"):
        read_portfolio_artifact(output, allowed_root=tmp_path, source_root=tmp_path / "source")


def test_decimal_nineteenth_place_roundtrips_and_one_e18_tamper_fails(tmp_path):
    result, sources = _build(tmp_path / "source")
    signal_path = tmp_path / "source" / "signals.parquet"
    old = pq.read_table(signal_path)
    score = Decimal("1.2345678901234567885")
    signal_table = pa.table({
        "date": old["date"],
        "code": old["code"],
        "factor": pa.array([score, None, None, None, None, None], type=pa.decimal128(38, 20)),
    })
    pq.write_table(signal_table, signal_path)
    sources = load_verified_portfolio_sources(
        dataset_manifest_path=tmp_path / "source" / "dataset" / "manifest.json",
        benchmark_receipt_path=tmp_path / "source" / "benchmark.json",
        signal_path=signal_path,
        signal_sha256=_file_hash(signal_path),
        training_artifact_id="training",
        training_artifact_hash="1" * 64,
        validation_artifact_id="validation",
        validation_artifact_hash="2" * 64,
        allowed_root=tmp_path / "source",
    )
    bundle = replace(
        _bundle(),
        dataset_content_hash=sources.input_manifest.dataset_content_hash,
        training_evidence_hash="1" * 64,
        validation_evidence_hash="2" * 64,
    )
    result = run_manual_daily_portfolio_v3(
        daily=sources.daily, eligibility=sources.eligibility, benchmark=sources.benchmark,
        signals=sources.signals, corporate_actions=sources.corporate_actions,
        calendar=sources.calendar, bundle=bundle, input_manifest=sources.input_manifest,
        start=sources.calendar.days[0], end=sources.calendar.days[-1], initial_capital=100_000,
    )
    output = result.write_evidence(tmp_path / "precision", sources=sources)
    assert read_portfolio_artifact(
        output, allowed_root=tmp_path, source_root=tmp_path / "source",
    ).replay["passed"]

    result.signals[0]["score"] = str(Decimal(result.signals[0]["score"]) + Decimal("1e-18"))
    failed = result.write_evidence(tmp_path / "precision-tampered", sources=sources)
    with pytest.raises(ValueError, match="semantic_replay_failed"):
        read_portfolio_artifact(failed, allowed_root=tmp_path, source_root=tmp_path / "source")


def test_artifact_roundtrip_rebuilds_company_action_rows(tmp_path):
    result, sources = _build(tmp_path / "source", with_action=True)
    output = result.write_evidence(tmp_path / "artifact", sources=sources)
    read = read_portfolio_artifact(output, allowed_root=tmp_path, source_root=tmp_path / "source")
    assert read.replay["passed"]
    assert any(row["stage"] == "ex_bonus" for row in read.result.corporate_actions)


def test_semantic_read_rejects_physically_valid_summary_tampering(tmp_path):
    result, sources = _build(tmp_path / "source")
    output = result.write_evidence(tmp_path / "artifact", sources=sources)
    summary_path = output / "summary.json"
    summary = json.loads(summary_path.read_text())
    summary["metrics"]["total_return"] = "999"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    entry = next(item for item in manifest["files"] if item["path"] == "summary.json")
    entry.update(_json_entry(summary_path, "summary"))
    manifest["manifest_hash"] = _hash({key: value for key, value in manifest.items() if key != "manifest_hash"})
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    assert verify_portfolio_evidence_directory(output)["verified"]
    with pytest.raises(ValueError, match="derived_report_mismatch"):
        read_portfolio_artifact(output, allowed_root=tmp_path, source_root=tmp_path / "source")


def test_reader_rejects_v1_without_envelope_and_failed_result(tmp_path):
    result, sources = _build(tmp_path / "source")
    output = result.write_evidence(tmp_path / "artifact", sources=sources)
    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["protocol_version"] = "manual-daily-portfolio-evidence-v1"
    manifest.pop("run_envelope")
    manifest["manifest_hash"] = _hash({key: value for key, value in manifest.items() if key != "manifest_hash"})
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    with pytest.raises(ValueError, match="run_envelope_required"):
        read_portfolio_artifact(output, allowed_root=tmp_path, source_root=tmp_path / "source")

    failed, failed_sources = _build(tmp_path / "failed-source")
    failed.quality_errors.append("forced_failure")
    failed_output = failed.write_evidence(tmp_path / "failed", sources=failed_sources)
    with pytest.raises(ValueError, match="semantic_replay_failed|quality_audit_failed|derived_report_mismatch"):
        read_portfolio_artifact(
            failed_output, allowed_root=tmp_path, source_root=tmp_path / "failed-source",
        )


def test_pair_rejects_reverse_scenario_and_capital_identity(tmp_path):
    baseline, sources = _build(tmp_path / "source")
    stress = _rerun(baseline, sources, scenario="stress")
    base_dir = baseline.write_evidence(tmp_path / "base", sources=sources)
    stress_dir = stress.write_evidence(tmp_path / "stress", sources=sources)
    with pytest.raises(ValueError, match="pair_scenarios_invalid"):
        audit_portfolio_pair(
            stress_dir, base_dir, allowed_root=tmp_path, source_root=tmp_path / "source",
        )

    other = _rerun(baseline, sources, scenario="stress", capital=200_000)
    other_dir = other.write_evidence(tmp_path / "other", sources=sources)
    with pytest.raises(ValueError, match="pair_non_cost_identity_mismatch"):
        audit_portfolio_pair(
            base_dir, other_dir, allowed_root=tmp_path, source_root=tmp_path / "source",
        )


def test_non_cent_initial_capital_is_rejected_before_rounding(tmp_path):
    result, sources = _build(tmp_path / "source", capital=Decimal("100000.001"))
    with pytest.raises(ValueError, match="portfolio_run_boundary_invalid"):
        result.write_evidence(tmp_path / "artifact", sources=sources)
