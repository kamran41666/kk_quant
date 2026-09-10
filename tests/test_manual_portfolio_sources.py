"""Controlled source manifest loading for manual portfolio v3."""
from datetime import date
import hashlib
import json

import pandas as pd
import pytest

from quant_engine.backtest.manual_portfolio_sources import load_verified_portfolio_sources


def _file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _sources(tmp_path):
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    days = [date(2027, 1, 4), date(2027, 1, 5), date(2027, 1, 6)]
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
        "universe": ["600000.SH"], "start_date": "2027-01-04", "end_date": "2027-01-06",
        "calendar_hash": calendar_hash, "files": {role: item["sha256"] for role, item in files.items()},
        "source_files": [],
    }
    manifest = {
        "dataset_id": "test/normalized-v2", "start_date": "2027-01-04", "end_date": "2027-01-06",
        "universe": ["600000.SH"], "calendar": {"content_hash": calendar_hash},
        "files": files, "source_files": [],
        "quality": [{"unexplained_reference_adjustments": ["x"]}],
        "content_hash": _hash(identity),
    }
    manifest_path = dataset / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    benchmark_path = tmp_path / "benchmark.parquet"
    pd.DataFrame({"date": days, "code": ["sh.000300"] * 3, "close": [100, 101, 102]}).to_parquet(benchmark_path, index=False)
    receipt_path = tmp_path / "benchmark.json"
    receipt_path.write_text(json.dumps({
        "arguments": {"code": "sh.000300"}, "sha256": _file_hash(benchmark_path),
    }), encoding="utf-8")
    signal_path = tmp_path / "signals.parquet"
    pd.DataFrame({"date": days, "code": ["600000.SH"] * 3, "factor": [1, 2, 3]}).to_parquet(signal_path, index=False)
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
