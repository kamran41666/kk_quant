"""Shared on-disk H2c economic fixture for API and promotion tests."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from server.config import settings
from server.services.manual_holdout import create_manual_holdout
from tests.manual_portfolio_test_support import _content_hash
from tests.test_manual_holdout import _ready
from tests.test_manual_portfolio_sources import _file_hash, _sources


def build_holdout_case(db, tmp_path: Path, monkeypatch, *, profitable: bool = True) -> dict:
    """Build a real parent release and an independent 70-session holdout source.

    The helper stops before freezing or running the evaluation so tests can
    assert the access-before-read boundary explicitly.
    """
    parent_fixture, release = _ready(db, tmp_path, monkeypatch)
    source_root = Path(tmp_path) / "holdout-source"
    source_root.mkdir(parents=True, exist_ok=True)
    manifest_path, receipt_path, _ = _sources(source_root, day_count=70, unexplained=False)
    dataset_dir = source_root / "dataset"
    daily_path = dataset_dir / "daily.parquet"
    daily = pd.read_parquet(daily_path)
    prices: list[float] = []
    for index in range(len(daily)):
        prices.append(round(10 * (1.01 ** index), 2) if profitable else 10.0)
    daily["open"] = prices
    daily["high"] = prices
    daily["low"] = prices
    daily["close"] = prices
    daily["adjusted_close"] = prices
    daily["vendor_adjusted_close"] = prices
    daily["preclose"] = [10.0] + prices[:-1]
    daily["source_return"] = [0.0] + [round((prices[i] / prices[i - 1]) - 1, 12) for i in range(1, len(prices))]
    daily["volume"] = 10_000_000
    daily["amount"] = [10_000_000 * price for price in prices]
    daily.to_parquet(daily_path, index=False)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["dataset_id"] = "holdout-evaluation-v1"
    manifest["files"]["daily"]["sha256"] = _file_hash(daily_path)
    manifest["content_hash"] = _content_hash(manifest)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    benchmark_path = source_root / "benchmark.parquet"
    benchmark = pd.read_parquet(benchmark_path)
    benchmark["close"] = 100.0
    benchmark.to_parquet(benchmark_path, index=False)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["sha256"] = _file_hash(benchmark_path)
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    binding = create_manual_holdout(
        db, release_id=release.id, dataset_id=manifest["dataset_id"],
        data_content_hash=manifest["content_hash"], start_date="2027-02-01",
        end_date="2027-02-12", actor="holdout-fixture", idempotency_key="holdout-fixture-create",
    )
    return {
        "binding": binding,
        "release": release,
        "parent_fixture": parent_fixture,
        "source_root": Path(tmp_path),
        "results_root": Path(settings.result_dir),
        "dataset_manifest_path": manifest_path,
        "benchmark_receipt_path": receipt_path,
        "freeze_kwargs": {
            "binding_id": binding.id,
            "actor": "holdout-fixture",
            "dataset_manifest_path": str(manifest_path.relative_to(tmp_path)),
            "benchmark_receipt_path": str(receipt_path.relative_to(tmp_path)),
            "idempotency_key": "holdout-fixture-evaluation",
        },
    }
