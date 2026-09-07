"""Versioned, Python-first strategy contract.

The spec is declarative metadata for discovery, validation and UI generation.
Executable trading logic stays in a :class:`Strategy` subclass; this module is
deliberately not a JSON/YAML programming language.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import json
import math
from types import MappingProxyType
from typing import Any, Mapping, Sequence


STRATEGY_PROTOCOL_VERSION = "2.0"
STRATEGY_PROTOCOL_ID = "kk-quant-strategy-v2"


class StrategyProtocolError(ValueError):
    """Raised when a strategy declaration or runtime output is invalid."""


class ParameterType(str, Enum):
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    STRING = "string"


@dataclass(frozen=True)
class ParameterSpec:
    key: str
    label: str
    type: ParameterType
    default: Any
    description: str = ""
    required: bool = False
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[Any, ...] = ()
    advanced: bool = False

    def __post_init__(self) -> None:
        if not self.key.isidentifier():
            raise StrategyProtocolError(f"invalid parameter key: {self.key!r}")
        self.validate(self.default)

    def validate(self, value: Any) -> Any:
        if self.type == ParameterType.INTEGER:
            if isinstance(value, bool) or not isinstance(value, int):
                raise StrategyProtocolError(f"parameter {self.key} must be an integer")
            normalized: Any = value
        elif self.type == ParameterType.NUMBER:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise StrategyProtocolError(f"parameter {self.key} must be a number")
            normalized = float(value)
            if not math.isfinite(normalized):
                raise StrategyProtocolError(f"parameter {self.key} must be finite")
        elif self.type == ParameterType.BOOLEAN:
            if not isinstance(value, bool):
                raise StrategyProtocolError(f"parameter {self.key} must be a boolean")
            normalized = value
        elif self.type == ParameterType.STRING:
            if not isinstance(value, str):
                raise StrategyProtocolError(f"parameter {self.key} must be a string")
            normalized = value
        else:  # pragma: no cover - Enum prevents this for normal construction
            raise StrategyProtocolError(f"unsupported parameter type: {self.type}")
        if self.minimum is not None and normalized < self.minimum:
            raise StrategyProtocolError(f"parameter {self.key} must be >= {self.minimum}")
        if self.maximum is not None and normalized > self.maximum:
            raise StrategyProtocolError(f"parameter {self.key} must be <= {self.maximum}")
        if self.choices and normalized not in self.choices:
            raise StrategyProtocolError(f"parameter {self.key} must be one of {list(self.choices)}")
        return normalized

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key, "label": self.label, "type": self.type.value,
            "default": self.default, "description": self.description,
            "required": self.required, "minimum": self.minimum,
            "maximum": self.maximum, "choices": list(self.choices),
            "advanced": self.advanced,
        }


@dataclass(frozen=True)
class DataRequirement:
    dataset: str
    fields: tuple[str, ...]
    lookback: int
    frequency: str = "1d"
    adjustment: str = "event_driven"
    optional: bool = False

    def __post_init__(self) -> None:
        if not self.dataset or not self.fields or self.lookback < 1:
            raise StrategyProtocolError("data requirement needs dataset, fields and lookback >= 1")

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset, "fields": list(self.fields),
            "lookback": self.lookback, "frequency": self.frequency,
            "adjustment": self.adjustment, "optional": self.optional,
        }


@dataclass(frozen=True)
class AnalysisOutputSpec:
    key: str
    label: str
    type: str = "number"
    description: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"key": self.key, "label": self.label, "type": self.type, "description": self.description}


@dataclass(frozen=True)
class StrategySpec:
    id: str
    name: str
    version: str
    description: str
    markets: tuple[str, ...]
    parameters: tuple[ParameterSpec, ...] = ()
    data: tuple[DataRequirement, ...] = ()
    rebalance_frequency: str = "weekly"
    warmup_bars: int = 0
    signal_type: str = "target_weights"
    long_only: bool = True
    max_gross_exposure: float = 1.0
    tags: tuple[str, ...] = ()
    analysis_outputs: tuple[AnalysisOutputSpec, ...] = ()
    research_document: str | None = None
    extensions: Mapping[str, Any] = field(default_factory=dict)
    protocol_version: str = STRATEGY_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if not self.id or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for ch in self.id):
            raise StrategyProtocolError("strategy id must be a lowercase slug")
        if not self.name or not self.version or not self.description or not self.markets:
            raise StrategyProtocolError("strategy spec requires name, version, description and markets")
        if self.rebalance_frequency not in {"daily", "weekly", "monthly"}:
            raise StrategyProtocolError("rebalance_frequency must be daily, weekly or monthly")
        if self.signal_type != "target_weights":
            raise StrategyProtocolError("protocol 2.0 supports target_weights output")
        if self.warmup_bars < 0 or not math.isfinite(self.max_gross_exposure) or self.max_gross_exposure <= 0:
            raise StrategyProtocolError("invalid warmup or exposure declaration")
        keys = [item.key for item in self.parameters]
        if len(keys) != len(set(keys)):
            raise StrategyProtocolError("parameter keys must be unique")
        analysis_keys = [item.key for item in self.analysis_outputs]
        if len(analysis_keys) != len(set(analysis_keys)):
            raise StrategyProtocolError("analysis output keys must be unique")
        # Freeze a defensive copy while retaining JSON-compatible extension points.
        try:
            json.dumps(dict(self.extensions))
        except (TypeError, ValueError) as exc:
            raise StrategyProtocolError("extensions must be JSON serializable") from exc
        object.__setattr__(self, "extensions", MappingProxyType(dict(self.extensions)))

    def validate_params(self, supplied: Mapping[str, Any] | None = None) -> dict[str, Any]:
        raw = dict(supplied or {})
        known = {item.key: item for item in self.parameters}
        unknown = sorted(set(raw) - set(known))
        if unknown:
            raise StrategyProtocolError(f"unknown strategy parameter(s): {', '.join(unknown)}")
        result: dict[str, Any] = {}
        for item in self.parameters:
            if item.key in raw:
                result[item.key] = item.validate(raw[item.key])
            elif item.required and item.default is None:
                raise StrategyProtocolError(f"missing required strategy parameter: {item.key}")
            else:
                result[item.key] = item.validate(item.default)
        return result

    def as_dict(self, implementation: str | None = None) -> dict[str, Any]:
        payload = {
            "id": self.id, "name": self.name, "version": self.version,
            "protocol_version": self.protocol_version,
            "description": self.description, "markets": list(self.markets),
            "parameters": [item.as_dict() for item in self.parameters],
            "data": [item.as_dict() for item in self.data],
            "execution": {
                "signal_type": self.signal_type,
                "rebalance_frequency": self.rebalance_frequency,
                "warmup_bars": self.warmup_bars,
                "long_only": self.long_only,
                "max_gross_exposure": self.max_gross_exposure,
            },
            "tags": list(self.tags),
            "analysis_outputs": [item.as_dict() for item in self.analysis_outputs],
            "research_document": self.research_document,
            "extensions": dict(self.extensions),
        }
        if implementation:
            payload["implementation"] = implementation
        return payload


@dataclass(frozen=True)
class StrategyOutput:
    target_weights: Mapping[str, float]
    diagnostics: Mapping[str, Any] = field(default_factory=dict)


def normalize_strategy_output(
    value: StrategyOutput | Mapping[str, float],
    *,
    spec: StrategySpec | None = None,
) -> StrategyOutput:
    """Normalize legacy dict output and enforce one shared engine contract."""
    output = value if isinstance(value, StrategyOutput) else StrategyOutput(target_weights=value)
    if not isinstance(output.target_weights, Mapping):
        raise StrategyProtocolError("generate_signals() must return StrategyOutput or a weight mapping")
    normalized: dict[str, float] = {}
    for code, raw_weight in output.target_weights.items():
        if not isinstance(code, str) or not code:
            raise StrategyProtocolError("target weight keys must be non-empty strings")
        if isinstance(raw_weight, bool) or not isinstance(raw_weight, (int, float)):
            raise StrategyProtocolError(f"target weight for {code} must be numeric")
        weight = float(raw_weight)
        if not math.isfinite(weight):
            raise StrategyProtocolError(f"target weight for {code} must be finite")
        if (spec is None or spec.long_only) and weight < 0:
            raise StrategyProtocolError(f"negative target weight is not allowed for {code}")
        normalized[code] = weight
    limit = spec.max_gross_exposure if spec else 1.0
    gross = sum(abs(weight) for weight in normalized.values())
    if gross > limit + 1e-9:
        raise StrategyProtocolError(f"gross target exposure {gross:.6f} exceeds {limit:.6f}")
    try:
        diagnostics = json.loads(json.dumps(dict(output.diagnostics), default=str))
    except (TypeError, ValueError) as exc:
        raise StrategyProtocolError("strategy diagnostics must be JSON serializable") from exc
    declared = {item.key for item in spec.analysis_outputs} if spec else set()
    if declared:
        unknown = sorted(set(diagnostics) - declared)
        if unknown:
            raise StrategyProtocolError(f"undeclared strategy diagnostic(s): {', '.join(unknown)}")
    return StrategyOutput(MappingProxyType(normalized), MappingProxyType(diagnostics))


STRATEGY_PROTOCOL = {
    "id": STRATEGY_PROTOCOL_ID,
    "version": STRATEGY_PROTOCOL_VERSION,
    "name": "统一策略协议",
    "supported_markets": ["a-share", "cn-fund"],
    "required_hooks": [
        {"name": "initialize", "purpose": "初始化仅属于策略的运行状态"},
        {"name": "generate_signals", "purpose": "输出目标仓位和可选诊断"},
    ],
    "lifecycle": ["initialize", "before_trading", "generate_signals", "on_rebalance", "on_order_filled", "teardown"],
    "spec_contract": "StrategySpec",
    "output_contract": "StrategyOutput[target_weights, diagnostics]",
    "discovery": "strategies package auto-discovery",
}
