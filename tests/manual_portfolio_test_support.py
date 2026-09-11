"""Shared real on-disk portfolio pair fixture for API and service tests."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pandas as pd

from quant_engine.backtest.manual_daily_portfolio_v3 import run_manual_daily_portfolio_v3
from quant_engine.backtest.manual_portfolio_sources import load_verified_portfolio_sources
from quant_engine.factor.manual_daily_label import MANUAL_DAILY_LABEL_V1
from quant_engine.trading.manual_protocol import stable_hash
from server.services.manual_evidence import register_factor_experiment_artifact
from tests.test_manual_daily_factor_research import _bundle
from tests.test_manual_evidence import _candidate, _experiment
from tests.test_manual_portfolio_sources import _file_hash, _sources


def _content_hash(manifest: dict) -> str:
    identity = {
        "universe": manifest["universe"],
        "start_date": manifest["start_date"],
        "end_date": manifest["end_date"],
        "calendar_hash": manifest["calendar"]["content_hash"],
        "files": {role: item["sha256"] for role, item in manifest["files"].items()},
        "source_files": manifest.get("source_files", []),
    }
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def build_portfolio_pair(db, tmp_path: Path, *, profitable: bool = True) -> dict:
    source_root = Path(tmp_path)
    results_root = source_root / "results"
    inputs = source_root / "inputs"
    results_root.mkdir(parents=True, exist_ok=True)
    inputs.mkdir(parents=True, exist_ok=True)
    (inputs / "factor-experiments").mkdir(parents=True, exist_ok=True)
    manifest_path, receipt_path, _ = _sources(inputs, day_count=8, unexplained=False)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    days = [pd.Timestamp(day).date() for day in pd.read_parquet(inputs / "dataset" / "calendar.parquet")["date"]]
    daily_path = inputs / "dataset" / "daily.parquet"
    daily = pd.read_parquet(daily_path)
    if profitable:
        daily[["close", "high"]] = daily[["close", "high"]].astype(float)
        daily.loc[daily["date"] == days[4], ["close", "high"]] = 10.5
        daily["preclose"] = daily["close"].shift(1).fillna(daily["preclose"])
    daily.to_parquet(daily_path, index=False)
    manifest["files"]["daily"]["sha256"] = _file_hash(daily_path)
    manifest["content_hash"] = _content_hash(manifest)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    benchmark_path = inputs / "benchmark.parquet"
    benchmark = pd.read_parquet(benchmark_path)
    benchmark["close"] = 100
    benchmark.to_parquet(benchmark_path, index=False)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["sha256"] = _file_hash(benchmark_path)
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    candidate = _candidate(db)
    training = _experiment(db, inputs / "factor-experiments", candidate, stage="training", start=str(days[0]), end=str(days[1]), decision="training_passed")
    validation = _experiment(db, inputs / "factor-experiments", candidate, stage="validation", start=str(days[2]), end=str(days[3]), decision="validation_passed")
    for experiment, values in (
        (training, [(days[0], "600000.SH", 1.0), (days[1], "600000.SH", 1.0)]),
        (validation, [(days[2], "600000.SH", 1.0), (days[3], "600000.SH", float("nan"))]),
    ):
        directory = Path(experiment.artifact_dir)
        frame = pd.DataFrame(values, columns=["date", "code", "factor"])
        frame["date"] = pd.to_datetime(frame["date"])
        frame.to_parquet(directory / "factor_values.parquet", index=False)
        report = json.loads((directory / "report.json").read_text(encoding="utf-8"))
        report.update({"dataset_id": manifest["dataset_id"], "data_content_hash": manifest["content_hash"]})
        (directory / "report.json").write_text(json.dumps(report, sort_keys=True), encoding="utf-8")
        experiment.dataset_id = manifest["dataset_id"]
        experiment.data_content_hash = manifest["content_hash"]
        experiment.result_json = json.dumps(report, sort_keys=True)
    db.commit()
    training_artifact = register_factor_experiment_artifact(db, training.id, allowed_root=inputs / "factor-experiments")
    validation_artifact = register_factor_experiment_artifact(db, validation.id, allowed_root=inputs / "factor-experiments")

    signal_path = Path(validation.artifact_dir) / "factor_values.parquet"
    sources = load_verified_portfolio_sources(
        dataset_manifest_path=manifest_path, benchmark_receipt_path=receipt_path,
        signal_path=signal_path, signal_sha256=_file_hash(signal_path),
        training_artifact_id=training_artifact.id, training_artifact_hash=training_artifact.evidence_hash,
        validation_artifact_id=validation_artifact.id, validation_artifact_hash=validation_artifact.evidence_hash,
        allowed_root=source_root,
    )
    bundle = replace(
        _bundle(), factor_expression_hash=candidate.expression_hash,
        label_spec_hash=stable_hash(MANUAL_DAILY_LABEL_V1.as_dict()),
        training_evidence_hash=training_artifact.evidence_hash,
        validation_evidence_hash=validation_artifact.evidence_hash,
        dataset_content_hash=sources.input_manifest.dataset_content_hash,
        cost_scenario="baseline",
    )
    baseline = run_manual_daily_portfolio_v3(
        daily=sources.daily, eligibility=sources.eligibility, benchmark=sources.benchmark,
        signals=sources.signals, corporate_actions=sources.corporate_actions, calendar=sources.calendar,
        bundle=bundle, input_manifest=sources.input_manifest, start=days[2], end=days[-1], initial_capital=100000,
    )
    stress = run_manual_daily_portfolio_v3(
        daily=sources.daily, eligibility=sources.eligibility, benchmark=sources.benchmark,
        signals=sources.signals, corporate_actions=sources.corporate_actions, calendar=sources.calendar,
        bundle=replace(bundle, cost_scenario="stress"), input_manifest=sources.input_manifest,
        start=days[2], end=days[-1], initial_capital=100000,
    )
    baseline_dir = baseline.write_evidence(results_root / "baseline", sources=sources)
    stress_dir = stress.write_evidence(results_root / "stress", sources=sources)
    return {
        "sources": sources, "bundle": bundle,
        "baseline_result": baseline, "stress_result": stress,
        "baseline_dir": baseline_dir, "stress_dir": stress_dir,
        "training_artifact": training_artifact, "validation_artifact": validation_artifact,
        "results_root": results_root, "source_root": source_root,
        "candidate": candidate, "training_experiment": training, "validation_experiment": validation,
    }
