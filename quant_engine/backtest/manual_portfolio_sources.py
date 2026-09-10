"""Verified file-backed inputs for manual-daily portfolio evidence."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
import json
from pathlib import Path
from typing import Any

import pandas as pd

from quant_engine.backtest.manual_daily_portfolio_v3 import ManualPortfolioInputManifest
from quant_engine.backtest.manual_research_inputs import load_research_corporate_actions
from quant_engine.backtest.manual_research_ledger import ResearchCorporateAction


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _controlled_file(configured: str | Path, manifest_dir: Path, root: Path) -> Path:
    supplied = Path(configured)
    candidates = (supplied, manifest_dir / supplied.name)
    path = next((item for item in candidates if item.exists()), None)
    if path is None:
        raise FileNotFoundError(f"verified_source_file_missing:{supplied.name}")
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"verified_source_file_invalid:{path.name}")
    resolved = path.resolve(strict=True)
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"verified_source_path_escape:{path.name}")
    return resolved


@dataclass(frozen=True)
class FrozenTradingCalendar:
    days: tuple[date, ...]
    content_hash: str

    def ensure_coverage(self, start: date, end: date) -> dict[str, Any]:
        return {
            "complete": bool(self.days and self.days[0] <= start <= end <= self.days[-1]),
            "content_hash": self.content_hash,
            "source": "verified_normalized_manifest",
        }

    def get_trading_days(self, start: date, end: date) -> list[date]:
        return [day for day in self.days if start <= day <= end]


@dataclass(frozen=True)
class VerifiedPortfolioSources:
    daily: pd.DataFrame
    eligibility: pd.DataFrame
    benchmark: pd.DataFrame
    signals: dict[date, dict[str, Any]]
    corporate_actions: tuple[ResearchCorporateAction, ...]
    calendar: FrozenTradingCalendar
    input_manifest: ManualPortfolioInputManifest
    source_root: Path
    signal_value_column: str = "factor"

    def verify_files(self) -> bool:
        try:
            for item in self.input_manifest.source_files:
                path = (self.source_root / str(item["path"])).resolve(strict=True)
                if path.is_symlink() or self.source_root not in path.parents:
                    return False
                if path.stat().st_size != int(item["size"]) or _hash_file(path) != item["sha256"]:
                    return False
        except (KeyError, OSError, TypeError, ValueError):
            return False
        return self.input_manifest.source_files_complete

    def source_path(self, role: str) -> Path:
        item = next((value for value in self.input_manifest.source_files if value.get("role") == role), None)
        if item is None:
            raise KeyError(f"verified_source_role_missing:{role}")
        path = (self.source_root / str(item["path"])).resolve(strict=True)
        if self.source_root not in path.parents or path.is_symlink():
            raise ValueError(f"verified_source_path_invalid:{role}")
        return path

    def reload(self) -> dict[str, Any]:
        if not self.verify_files():
            raise ValueError("verified_source_files_changed")
        daily = pd.read_parquet(self.source_path("daily"))
        securities = pd.read_parquet(self.source_path("securities"))
        actions_frame = pd.read_parquet(self.source_path("actions"))
        signal_frame = pd.read_parquet(self.source_path("signals"))
        signals: dict[date, dict[str, Any]] = {}
        for row in signal_frame[["date", "code", self.signal_value_column]].to_dict("records"):
            if pd.isna(row[self.signal_value_column]):
                continue
            signals.setdefault(pd.Timestamp(row["date"]).date(), {})[str(row["code"]).upper()] = row[self.signal_value_column]
        return {
            "daily": daily,
            "eligibility": _eligibility(daily, securities),
            "actions": tuple(load_research_corporate_actions(
                actions_frame, source_hash=self.input_manifest.corporate_action_content_hash,
            )),
            "calendar": tuple(pd.Timestamp(value).date() for value in pd.read_parquet(self.source_path("calendar"))["date"].tolist()),
            "benchmark": pd.read_parquet(self.source_path("benchmark"))[["date", "close"]],
            "signals": signals,
        }


def _source_entry(role: str, path: Path, root: Path) -> dict[str, Any]:
    return {
        "role": role, "path": path.relative_to(root).as_posix(),
        "size": path.stat().st_size, "sha256": _hash_file(path),
    }


def _eligibility(daily: pd.DataFrame, securities: pd.DataFrame) -> pd.DataFrame:
    required = {"code", "date", "close", "is_suspended", "is_st"}
    if required - set(daily.columns) or {"code", "ipo_date", "out_date"} - set(securities.columns):
        raise ValueError("normalized_eligibility_fields_missing")
    frame = daily[["code", "date", "close", "is_suspended", "is_st"]].copy()
    frame["date"] = pd.to_datetime(frame["date"])
    master = securities[["code", "ipo_date", "out_date"]].copy()
    master["ipo_date"] = pd.to_datetime(master["ipo_date"], errors="coerce")
    master["out_date"] = pd.to_datetime(master["out_date"], errors="coerce")
    frame = frame.merge(master, on="code", how="left", validate="many_to_one")
    frame["is_eligible"] = (
        frame["close"].notna() & (frame["close"] > 0)
        & ~frame["is_suspended"].astype(bool) & ~frame["is_st"].astype(bool)
        & frame["ipo_date"].notna() & ((frame["date"] - frame["ipo_date"]).dt.days >= 180)
        & (frame["out_date"].isna() | (frame["date"] < frame["out_date"]))
    )
    return frame[["date", "code", "is_eligible"]]


def load_verified_portfolio_sources(
    *,
    dataset_manifest_path: str | Path,
    benchmark_receipt_path: str | Path,
    signal_path: str | Path,
    signal_sha256: str,
    training_artifact_id: str,
    training_artifact_hash: str,
    validation_artifact_id: str,
    validation_artifact_hash: str,
    allowed_root: str | Path,
    signal_value_column: str = "factor",
) -> VerifiedPortfolioSources:
    root = Path(allowed_root).resolve(strict=True)
    dataset_manifest_file = _controlled_file(dataset_manifest_path, Path(dataset_manifest_path).parent, root)
    benchmark_receipt_file = _controlled_file(benchmark_receipt_path, Path(benchmark_receipt_path).parent, root)
    signal_file = _controlled_file(signal_path, Path(signal_path).parent, root)
    try:
        dataset_manifest = json.loads(dataset_manifest_file.read_text(encoding="utf-8"))
        benchmark_receipt = json.loads(benchmark_receipt_file.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("verified_source_manifest_invalid") from exc
    files = dataset_manifest.get("files")
    if not isinstance(files, dict) or set(files) != {"daily", "actions", "securities", "calendar"}:
        raise ValueError("normalized_manifest_file_set_invalid")
    resolved = {}
    source_entries = []
    for role in ("daily", "actions", "securities", "calendar"):
        item = files[role]
        path = _controlled_file(item["path"], dataset_manifest_file.parent, root)
        actual = _hash_file(path)
        if actual != item.get("sha256"):
            raise ValueError(f"normalized_source_hash_mismatch:{role}")
        resolved[role] = path
        source_entries.append(_source_entry(role, path, root))
    identity = {
        "universe": dataset_manifest.get("universe"),
        "start_date": dataset_manifest.get("start_date"),
        "end_date": dataset_manifest.get("end_date"),
        "calendar_hash": (dataset_manifest.get("calendar") or {}).get("content_hash"),
        "files": {role: files[role]["sha256"] for role in files},
        "source_files": dataset_manifest.get("source_files"),
    }
    if _hash(identity) != dataset_manifest.get("content_hash"):
        raise ValueError("normalized_manifest_content_hash_mismatch")
    benchmark_path = _controlled_file(
        benchmark_receipt_file.with_suffix(".parquet"), benchmark_receipt_file.parent, root,
    )
    if _hash_file(benchmark_path) != benchmark_receipt.get("sha256"):
        raise ValueError("benchmark_source_hash_mismatch")
    if _hash_file(signal_file) != signal_sha256:
        raise ValueError("signal_source_hash_mismatch")
    source_entries.extend((
        _source_entry("benchmark", benchmark_path, root),
        _source_entry("signals", signal_file, root),
    ))
    daily = pd.read_parquet(resolved["daily"])
    actions_frame = pd.read_parquet(resolved["actions"])
    securities = pd.read_parquet(resolved["securities"])
    calendar_frame = pd.read_parquet(resolved["calendar"])
    benchmark = pd.read_parquet(benchmark_path)[["date", "close"]]
    signal_frame = pd.read_parquet(signal_file)
    if {"date", "code", signal_value_column} - set(signal_frame.columns):
        raise ValueError("signal_source_fields_missing")
    if signal_frame.duplicated(["date", "code"]).any():
        raise ValueError("signal_source_rows_not_unique")
    signals: dict[date, dict[str, Any]] = {}
    for row in signal_frame[["date", "code", signal_value_column]].to_dict("records"):
        if pd.isna(row[signal_value_column]):
            continue
        signals.setdefault(pd.Timestamp(row["date"]).date(), {})[str(row["code"]).upper()] = row[signal_value_column]
    days = tuple(pd.Timestamp(value).date() for value in calendar_frame["date"].tolist())
    calendar_hash = str((dataset_manifest.get("calendar") or {}).get("content_hash"))
    unexplained = sum(len(item.get("unexplained_reference_adjustments", [])) for item in dataset_manifest.get("quality", []))
    manifest = ManualPortfolioInputManifest(
        dataset_id=str(dataset_manifest["dataset_id"]),
        dataset_content_hash=str(dataset_manifest["content_hash"]),
        calendar_content_hash=calendar_hash,
        signal_content_hash=signal_sha256,
        eligibility_content_hash=_hash({
            "daily": files["daily"]["sha256"], "securities": files["securities"]["sha256"],
            "policy": "ipo-180d-active-non-st-non-suspended-v1",
        }),
        corporate_action_content_hash=files["actions"]["sha256"],
        benchmark_id=str((benchmark_receipt.get("arguments") or {}).get("code") or "unknown"),
        benchmark_content_hash=str(benchmark_receipt["sha256"]),
        training_artifact_id=training_artifact_id,
        validation_artifact_id=validation_artifact_id,
        training_artifact_hash=training_artifact_hash,
        validation_artifact_hash=validation_artifact_hash,
        source_files=tuple(sorted(source_entries, key=lambda item: item["role"])),
        unresolved_adjustment_count=unexplained,
    )
    result = VerifiedPortfolioSources(
        daily=daily, eligibility=_eligibility(daily, securities), benchmark=benchmark,
        signals=signals,
        corporate_actions=tuple(load_research_corporate_actions(actions_frame, source_hash=files["actions"]["sha256"])),
        calendar=FrozenTradingCalendar(days, calendar_hash), input_manifest=manifest,
        source_root=root, signal_value_column=signal_value_column,
    )
    if not result.verify_files():
        raise ValueError("verified_source_files_changed_during_load")
    return result


__all__ = [
    "FrozenTradingCalendar", "VerifiedPortfolioSources",
    "load_verified_portfolio_sources",
]
