"""Controlled source manifest loading for manual portfolio v3."""
from datetime import date
from dataclasses import replace
from datetime import timedelta
import hashlib
import json

import pandas as pd
import pytest

from quant_engine.backtest.manual_portfolio_sources import load_verified_portfolio_sources
from quant_engine.backtest.manual_daily_portfolio_v3 import run_manual_daily_portfolio_v3
from tests.test_manual_daily_factor_research import _bundle


def _file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _sources(tmp_path, *, day_count=3, unexplained=True):
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    days = []
    cursor = date(2027, 1, 4)
    while len(days) < day_count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    daily = pd.DataFrame([{
        "code": "600000.SH", "date": day, "open": 10, "high": 10, "low": 10,
        "close": 10, "preclose": 10, "volume": 1_000_000, "amount": 10_000_000,
        "turnover_rate": 0.1, "is_suspended": False, "is_st": False,
        "adjusted_close": 10, "vendor_adjusted_close": 10, "source_return": 0,
    } for day in days])
    actions = pd.DataFrame(columns=[
        "code", "record_date", "ex_date", "pay_date", "stock_date",
        "cash_ps", "bonus_ratio", "bonus_allocation_verified",
    ])
    securities = pd.DataFrame([{"code": "600000.SH", "name": "test", "ipo_date": "2000-01-01", "out_date": None}])
    calendar = pd.DataFrame({"date": days})
    frames = {"daily": daily, "actions": actions, "securities": securities, "calendar": calendar}
    files = {}
    for role, frame in frames.items():
        path = dataset / f"{role}.parquet"
        frame.to_parquet(path, index=False)
        files[role] = {"path": str(path.resolve()), "sha256": _file_hash(path)}
    calendar_hash = "a" * 64
    identity = {
        "universe": ["600000.SH"], "start_date": days[0].isoformat(), "end_date": days[-1].isoformat(),
        "calendar_hash": calendar_hash, "files": {role: item["sha256"] for role, item in files.items()},
        "source_files": [],
    }
    manifest = {
        "dataset_id": "test/normalized-v2", "start_date": days[0].isoformat(), "end_date": days[-1].isoformat(),
        "universe": ["600000.SH"], "calendar": {"content_hash": calendar_hash},
        "files": files, "source_files": [],
        "quality": [{"unexplained_reference_adjustments": ["x"] if unexplained else []}],
        "content_hash": _hash(identity),
    }
    manifest_path = dataset / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    benchmark_path = tmp_path / "benchmark.parquet"
    pd.DataFrame({"date": days, "code": ["sh.000300"] * len(days), "close": [100 + index for index in range(len(days))]}).to_parquet(benchmark_path, index=False)
    receipt_path = tmp_path / "benchmark.json"
    receipt_path.write_text(json.dumps({
        "arguments": {"code": "sh.000300"}, "sha256": _file_hash(benchmark_path),
    }), encoding="utf-8")
    signal_path = tmp_path / "signals.parquet"
    pd.DataFrame({"date": days, "code": ["600000.SH"] * len(days), "factor": [1] + [float("nan")] * (len(days) - 1)}).to_parquet(signal_path, index=False)
    return manifest_path, receipt_path, signal_path


def test_verified_sources_bind_files_eligibility_actions_and_known_gaps(tmp_path):
    manifest_path, receipt_path, signal_path = _sources(tmp_path)
    sources = load_verified_portfolio_sources(
        dataset_manifest_path=manifest_path, benchmark_receipt_path=receipt_path,
        signal_path=signal_path, signal_sha256=_file_hash(signal_path),
        training_artifact_id="training", training_artifact_hash="1" * 64,
        validation_artifact_id="validation", validation_artifact_hash="2" * 64,
        allowed_root=tmp_path,
    )
    assert sources.verify_files() is True
    assert sources.input_manifest.source_files_complete is True
    assert sources.input_manifest.unresolved_adjustment_count == 1
    assert sources.eligibility["is_eligible"].tolist() == [True, True, True]
    assert sources.corporate_actions == ()
    assert sources.input_manifest.benchmark_id == "sh.000300"
    signal_path.write_bytes(b"tampered")
    assert sources.verify_files() is False


def test_source_loader_rejects_manifest_or_file_hash_changes(tmp_path):
    manifest_path, receipt_path, signal_path = _sources(tmp_path)
    manifest = json.loads(manifest_path.read_text())
    manifest["content_hash"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="manifest_content_hash_mismatch"):
        load_verified_portfolio_sources(
            dataset_manifest_path=manifest_path, benchmark_receipt_path=receipt_path,
            signal_path=signal_path, signal_sha256=_file_hash(signal_path),
            training_artifact_id="training", training_artifact_hash="1" * 64,
            validation_artifact_id="validation", validation_artifact_hash="2" * 64,
            allowed_root=tmp_path,
        )


def test_source_replay_rechecks_prices_capacity_eligibility_signals_and_benchmark(tmp_path):
    manifest_path, receipt_path, signal_path = _sources(tmp_path, day_count=6, unexplained=False)
    sources = load_verified_portfolio_sources(
        dataset_manifest_path=manifest_path, benchmark_receipt_path=receipt_path,
        signal_path=signal_path, signal_sha256=_file_hash(signal_path),
        training_artifact_id="training", training_artifact_hash="1" * 64,
        validation_artifact_id="validation", validation_artifact_hash="2" * 64,
        allowed_root=tmp_path,
    )
    bundle = replace(
        _bundle(), dataset_content_hash=sources.input_manifest.dataset_content_hash,
        training_evidence_hash="1" * 64, validation_evidence_hash="2" * 64,
    )
    result = run_manual_daily_portfolio_v3(
        daily=sources.daily, eligibility=sources.eligibility, benchmark=sources.benchmark,
        signals=sources.signals, corporate_actions=sources.corporate_actions,
        calendar=sources.calendar, bundle=bundle, input_manifest=sources.input_manifest,
        start=sources.calendar.days[0], end=sources.calendar.days[-1], initial_capital=100_000,
    )
    replay = result.replay(sources)
    assert replay["accounting_passed"] is True
    assert replay["source_and_execution_passed"] is True
    assert replay["target_completion_verifiable"] is True
    assert replay["passed"] is True
    output = result.write_evidence(tmp_path / "verified-output", sources=sources)
    written = json.loads((output / "manifest.json").read_text())
    assert written["eligible_for_artifact_registration"] is True
    sources.daily.loc[0, "close"] = 999  # in-memory mutation is ignored; replay reloads the hashed source file
    assert result.replay(sources)["passed"] is True
    signal_path.write_bytes(b"tampered")
    replay = result.replay(sources)
    assert replay["source_and_execution_passed"] is False
    assert replay["passed"] is False
