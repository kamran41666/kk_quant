"""Deterministic evidence helpers for backtests and paper observations.

The observation service must never infer that a completed backtest applies to
another market or to a later revision of a strategy.  This module keeps the
fingerprint and manifest format small, deterministic, and independent of any
network provider so it can be checked both at API boundaries and in tests.
"""
from __future__ import annotations

import hashlib
import inspect
import json
from datetime import date
from importlib import import_module
from pathlib import Path
from typing import Any, Mapping, Optional

from quant_engine.backtest.protocol import StrategyProtocolError, StrategySpec


EVIDENCE_VERSION = "backtest-evidence-v1"
CALENDAR_VERSION = "trading-calendar-v1"
# The matcher consumes the next valid session's opening price. Keep the old
# close-labelled value as a readable legacy version, but never emit it for a
# new run.
EXECUTION_MODEL = "next_trading_day_open-v1"
LEGACY_EXECUTION_MODELS = frozenset({"next_trading_day_close-v1"})
FUND_CALENDAR_VERSION = "cn-fund-nav-calendar-v1"
FUND_EXECUTION_MODEL = "next_valid_nav-v1"


def _params_object(params: Any) -> Any:
    if isinstance(params, str):
        try:
            params = json.loads(params or "{}")
        except (TypeError, ValueError):
            params = {"raw": params}
    return params or {}


def _strategy_spec(strategy_class: str) -> StrategySpec | None:
    try:
        module_name, class_name = str(strategy_class).rsplit(".", 1)
        if not module_name.startswith("strategies."):
            return None
        module = import_module(module_name)
        strategy_type = getattr(module, class_name)
    except (AttributeError, ImportError, TypeError, ValueError):
        return None
    spec = getattr(strategy_type, "SPEC", None)
    return spec if isinstance(spec, StrategySpec) else None


def normalized_strategy_params(strategy_class: str, params: Any) -> dict[str, Any]:
    """Return the exact parameter object bound into strategy evidence."""
    raw = _params_object(params)
    if not isinstance(raw, Mapping):
        raise StrategyProtocolError("strategy parameters must be a JSON object")
    spec = _strategy_spec(strategy_class)
    return spec.validate_params(raw) if spec is not None else dict(raw)


def _canonical_params(strategy_class: str, params: Any) -> str:
    normalized = normalized_strategy_params(strategy_class, params)
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def strategy_spec_identity(strategy_class: str) -> dict[str, str] | None:
    """Return stable declarative identity without executable strategy details."""
    spec = _strategy_spec(strategy_class)
    if spec is None:
        return None
    return {
        "id": spec.id,
        "version": spec.version,
        "protocol_version": spec.protocol_version,
    }


def strategy_source_hash(strategy_class: str) -> Optional[str]:
    """Return the source hash for a local strategy, or ``None`` if unavailable."""
    try:
        module_name, class_name = str(strategy_class).rsplit(".", 1)
        if not module_name.startswith("strategies."):
            return None
        module = import_module(module_name)
        strategy_type = getattr(module, class_name)
        source = inspect.getsource(strategy_type)
    except (AttributeError, ImportError, OSError, TypeError, ValueError):
        return None
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def strategy_research_gate_passed(
    strategy_class: str,
    params: Any = None,
    manifest: Any = None,
    allow_failed_performance: bool = False,
) -> bool:
    """Fail closed when a strategy declares an external research gate."""
    spec = _strategy_spec(strategy_class)
    if spec is None or not bool(spec.extensions.get("research_gate_required")):
        return True
    relative = spec.extensions.get("research_gate_result")
    if not isinstance(relative, str):
        return False
    repository = Path(__file__).resolve().parents[2]
    target = (repository / relative).resolve()
    research_root = (repository / "docs" / "research-runs").resolve()
    if research_root not in target.parents:
        return False
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
        frozen_path = target.with_name(target.name.replace("-results.json", "-frozen-evidence.json"))
        frozen = json.loads(frozen_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return False
    checks = (payload.get("research_gate") or {}).get("checks")
    expected = frozen.get("expected") if isinstance(frozen, dict) else None
    if not isinstance(checks, dict) or not checks:
        return False
    integrity_checks = {
        "share_adjusted_volume_verified",
        "historical_price_limit_and_suspension_verified",
    }
    if allow_failed_performance and not all(
        checks.get(key) is True for key in integrity_checks
    ):
        return False
    if not allow_failed_performance and not all(value is True for value in checks.values()):
        return False
    if not isinstance(expected, dict):
        return False
    result_evidence = {
        key: payload.get(key)
        for key in (
            "dataset_id", "dataset_hash", "coverage_hash", "calendar_source",
            "calendar_content_hash", "protocol_sha256", "strategy_source_hash",
            "execution_code_hashes",
        )
    }
    if result_evidence != expected:
        return False
    repository = Path(__file__).resolve().parents[2]
    protocol_path = target.with_name(target.name.replace("-results.json", "-protocol.md"))
    try:
        if hashlib.sha256(protocol_path.read_bytes()).hexdigest() != expected.get("protocol_sha256"):
            return False
    except OSError:
        return False
    for relative_path, expected_hash in (expected.get("execution_code_hashes") or {}).items():
        code_path = (repository / relative_path).resolve()
        if repository not in code_path.parents:
            return False
        try:
            actual_hash = hashlib.sha256(code_path.read_bytes()).hexdigest()
        except OSError:
            return False
        if actual_hash != expected_hash:
            return False
    try:
        normalized_params = normalized_strategy_params(strategy_class, params)
    except StrategyProtocolError:
        return False
    if isinstance(manifest, str):
        try:
            manifest = json.loads(manifest)
        except (TypeError, ValueError):
            return False
    if not isinstance(manifest, Mapping):
        return False
    runtime_coverage = manifest.get("daily_data_coverage")
    runtime_calendar = manifest.get("calendar_evidence")
    authorized = payload.get("authorized_observation_evidence")
    if (
        not isinstance(runtime_coverage, Mapping)
        or not isinstance(runtime_calendar, Mapping)
        or not isinstance(authorized, Mapping)
    ):
        return False
    runtime_evidence = {
        "dataset_hash": runtime_coverage.get("dataset_hash"),
        "coverage_hash": runtime_coverage.get("coverage_hash"),
        "cost_scenario": manifest.get("cost_scenario"),
        "calendar_source": runtime_calendar.get("source"),
        "calendar_content_hash": runtime_calendar.get("content_hash"),
        "universe": sorted(
            str(item.get("code"))
            for item in runtime_coverage.get("items", [])
            if isinstance(item, Mapping) and item.get("code")
        ),
    }
    expected_runtime = {
        "dataset_hash": authorized.get("dataset_hash"),
        "coverage_hash": authorized.get("coverage_hash"),
        "cost_scenario": authorized.get("cost_scenario"),
        "calendar_source": payload.get("calendar_source"),
        "calendar_content_hash": payload.get("calendar_content_hash"),
        "universe": sorted(str(code) for code in authorized.get("universe", [])),
    }
    return bool(
        runtime_evidence == expected_runtime
        and payload.get("strategy_id") == spec.id
        and payload.get("strategy_version") == spec.version
        and payload.get("strategy_source_hash") == strategy_source_hash(strategy_class)
        and payload.get("strategy_parameters") == normalized_params
        and (
            allow_failed_performance
            or (payload.get("research_gate") or {}).get("passed") is True
        )
    )


def strategy_fingerprint(strategy_class: str, params: Any) -> str:
    """Hash implementation, Spec identity, normalized parameters and source."""
    payload = {
        "strategy_class": str(strategy_class),
        "strategy_spec": strategy_spec_identity(strategy_class),
        "params": _canonical_params(strategy_class, params),
        "source_hash": strategy_source_hash(strategy_class),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def build_manifest(
    *,
    market: str,
    strategy_class: str,
    params: Any,
    start_date: date | str,
    end_date: date | str,
    benchmark: Optional[str],
    rebalance_frequency: str,
    universe: Optional[list[str]] = None,
    dataset_manifests: Optional[list[Mapping[str, Any]]] = None,
    nav_rule: Optional[str] = None,
    cost_scenario: Optional[str] = None,
) -> dict[str, Any]:
    """Build a serializable, point-in-time description of a backtest run."""
    fingerprint = strategy_fingerprint(strategy_class, params)
    start = start_date.isoformat() if isinstance(start_date, date) else str(start_date)
    end = end_date.isoformat() if isinstance(end_date, date) else str(end_date)
    normalized_market = str(market)
    is_fund = normalized_market == "cn-fund"
    manifest = {
        "evidence_version": EVIDENCE_VERSION,
        "market": str(market),
        "strategy_class": str(strategy_class),
        "strategy_spec": strategy_spec_identity(strategy_class),
        "strategy_parameters": normalized_strategy_params(strategy_class, params),
        "strategy_fingerprint": fingerprint,
        "start_date": start,
        "end_date": end,
        "data_end": end,
        "benchmark": benchmark,
        "rebalance_frequency": str(rebalance_frequency),
        "calendar_version": FUND_CALENDAR_VERSION if is_fund else CALENDAR_VERSION,
        "execution_model": FUND_EXECUTION_MODEL if is_fund else EXECUTION_MODEL,
        "data_source": "eastmoney:fund_nav_archive" if is_fund else ("local_a_share_store" if market == "a-share" else "unconfigured"),
        "source_hash": strategy_source_hash(strategy_class),
    }
    if universe is not None:
        manifest["universe"] = sorted({str(item) for item in universe})
    if dataset_manifests is not None:
        manifest["dataset_manifests"] = [dict(item) for item in dataset_manifests]
    if nav_rule is not None:
        manifest["nav_rule"] = str(nav_rule)
    if cost_scenario is not None:
        manifest["cost_scenario"] = str(cost_scenario)
    return manifest


def serialize_manifest(manifest: Mapping[str, Any]) -> str:
    return json.dumps(dict(manifest), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def manifest_is_complete(manifest: Any) -> bool:
    if isinstance(manifest, str):
        try:
            manifest = json.loads(manifest)
        except (TypeError, ValueError):
            return False
    if not isinstance(manifest, Mapping):
        return False
    required = {
        "evidence_version", "market", "strategy_fingerprint", "start_date",
        "end_date", "data_end", "calendar_version", "execution_model",
    }
    if not (required.issubset(manifest) and all(manifest.get(key) not in (None, "") for key in required)):
        return False
    # A run cannot authorize paper observation without binding the exact
    # verified exchange calendar used by its engine.  Legacy manifests that
    # predate this field remain readable, but are deliberately incomplete and
    # therefore cannot be promoted to a new observation.
    calendar_evidence = manifest.get("calendar_evidence")
    if manifest.get("market") in {"a-share", "cn-fund"}:
        if not isinstance(calendar_evidence, Mapping):
            return False
        content_hash = calendar_evidence.get("content_hash")
        if (
            not calendar_evidence.get("source")
            or calendar_evidence.get("verified") is not True
            or not isinstance(content_hash, str)
            or len(content_hash) != 64
        ):
            return False
    if manifest.get("market") == "cn-fund":
        datasets = manifest.get("dataset_manifests")
        return (
            manifest.get("data_source") == "eastmoney:fund_nav_archive"
            and manifest.get("execution_model") == FUND_EXECUTION_MODEL
            and manifest.get("nav_rule") == "published_nav_next_valid_day"
            and isinstance(datasets, list) and bool(datasets)
            and all(isinstance(item, Mapping) and item.get("dataset_id") and item.get("content_hash") for item in datasets)
        )
    if manifest.get("market") == "a-share":
        return manifest.get("execution_model") in {EXECUTION_MODEL, *LEGACY_EXECUTION_MODELS}
    return True
