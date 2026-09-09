"""Restricted, deterministic factor expression protocol.

Expressions are data, not Python source.  The interpreter exposes a small
allowlist over the project's point-in-time panel operators and never calls
``eval`` or imports model-provided code.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import re
from typing import Any, Mapping

import numpy as np
import pandas as pd

from quant_engine.factor import operators as op


EXPRESSION_PROTOCOL_VERSION = "1.0"
ALLOWED_FIELDS = frozenset({
    "open", "high", "low", "close", "volume", "amount", "turnover_rate",
})
UNARY_OPERATORS = frozenset({"abs", "inverse", "log", "rank", "demean", "scale"})
BINARY_OPERATORS = frozenset({"add", "sub", "mul", "div"})
PERIOD_OPERATORS = frozenset({"delay", "delta"})
ROLLING_OPERATORS = frozenset({
    "ts_mean", "ts_std", "ts_max", "ts_min", "ts_sum", "ts_skewness",
    "ts_kurtosis", "decay_linear", "ts_slope",
})
PAIR_ROLLING_OPERATORS = frozenset({"ts_corr", "ts_cov"})
ALL_OPERATORS = (
    UNARY_OPERATORS | BINARY_OPERATORS | PERIOD_OPERATORS | ROLLING_OPERATORS
    | PAIR_ROLLING_OPERATORS
    | {"signed_power", "ts_quantile", "winsorize_zscore"}
)
MAX_DEPTH = 8
MAX_NODES = 64
MAX_WINDOW = 504
MAX_ABS_CONSTANT = 1_000_000.0
_SLUG = re.compile(r"^[a-z][a-z0-9_]{2,79}$")


class FactorExpressionError(ValueError):
    """Raised when an expression cannot be safely validated or evaluated."""


@dataclass(frozen=True)
class ExpressionNode:
    kind: str
    name: str | None = None
    value: float | None = None
    args: tuple["ExpressionNode", ...] = ()
    params: tuple[tuple[str, float | int], ...] = ()

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        depth: int = 1,
        counter: list[int] | None = None,
    ) -> "ExpressionNode":
        if not isinstance(payload, Mapping):
            raise FactorExpressionError("expression node must be an object")
        if depth > MAX_DEPTH:
            raise FactorExpressionError(f"expression depth exceeds {MAX_DEPTH}")
        counter = counter if counter is not None else [0]
        counter[0] += 1
        if counter[0] > MAX_NODES:
            raise FactorExpressionError(f"expression node count exceeds {MAX_NODES}")

        keys = set(payload)
        if "field" in payload:
            if keys != {"field"} or payload["field"] not in ALLOWED_FIELDS:
                raise FactorExpressionError("field node must contain one allowed field")
            return cls(kind="field", name=str(payload["field"]))
        if "constant" in payload:
            if keys != {"constant"}:
                raise FactorExpressionError("constant node cannot contain extra keys")
            value = payload["constant"]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise FactorExpressionError("constant must be numeric")
            numeric = float(value)
            if not math.isfinite(numeric) or abs(numeric) > MAX_ABS_CONSTANT:
                raise FactorExpressionError("constant is non-finite or outside the allowed range")
            return cls(kind="constant", value=numeric)
        if "op" not in payload:
            raise FactorExpressionError("node must declare field, constant or op")
        if keys - {"op", "args", "params"}:
            raise FactorExpressionError("operator node contains unknown keys")
        name = payload["op"]
        if name not in ALL_OPERATORS:
            raise FactorExpressionError(f"operator is not allowed: {name!r}")
        raw_args = payload.get("args", [])
        raw_params = payload.get("params", {})
        if not isinstance(raw_args, list) or not isinstance(raw_params, Mapping):
            raise FactorExpressionError("operator args must be a list and params an object")
        args = tuple(
            cls.from_dict(item, depth=depth + 1, counter=counter) for item in raw_args
        )
        params = cls._validate_operator(name, args, raw_params)
        return cls(kind="operator", name=name, args=args, params=tuple(sorted(params.items())))

    @staticmethod
    def _validate_operator(
        name: str,
        args: tuple["ExpressionNode", ...],
        raw_params: Mapping[str, Any],
    ) -> dict[str, float | int]:
        arity = 2 if name in BINARY_OPERATORS | PAIR_ROLLING_OPERATORS else 1
        if len(args) != arity:
            raise FactorExpressionError(f"operator {name} requires {arity} argument(s)")
        if all(item.kind == "constant" for item in args):
            raise FactorExpressionError("an operator must depend on at least one market field")

        expected: set[str] = set()
        normalized: dict[str, float | int] = {}
        if name in PERIOD_OPERATORS:
            expected = {"periods"}
            periods = ExpressionNode._integer(raw_params.get("periods"), "periods")
            if periods < 1 or periods > MAX_WINDOW:
                raise FactorExpressionError(f"invalid periods for {name}")
            normalized["periods"] = periods
        elif name in ROLLING_OPERATORS | PAIR_ROLLING_OPERATORS | {"ts_quantile"}:
            expected = {"window", "min_periods"}
            if name == "ts_quantile":
                expected.add("quantile")
            window = ExpressionNode._integer(raw_params.get("window"), "window")
            minimum_default = max(2 if name in PAIR_ROLLING_OPERATORS else 1, math.ceil(window * 0.6))
            minimum = ExpressionNode._integer(
                raw_params.get("min_periods", minimum_default), "min_periods"
            )
            if not 1 <= window <= MAX_WINDOW or not 1 <= minimum <= window:
                raise FactorExpressionError("rolling window/min_periods are outside the allowed range")
            if name in PAIR_ROLLING_OPERATORS and window < 2:
                raise FactorExpressionError(f"operator {name} requires window >= 2")
            normalized.update(window=window, min_periods=minimum)
            if name == "ts_quantile":
                quantile = ExpressionNode._number(raw_params.get("quantile"), "quantile")
                if not 0 <= quantile <= 1:
                    raise FactorExpressionError("quantile must be in [0, 1]")
                normalized["quantile"] = quantile
        elif name == "signed_power":
            expected = {"exponent"}
            exponent = ExpressionNode._number(raw_params.get("exponent"), "exponent")
            if not -8 <= exponent <= 8:
                raise FactorExpressionError("signed_power exponent is outside [-8, 8]")
            normalized["exponent"] = exponent
        elif name == "winsorize_zscore":
            expected = {"lower", "upper"}
            lower = ExpressionNode._number(raw_params.get("lower", 0.01), "lower")
            upper = ExpressionNode._number(raw_params.get("upper", 0.99), "upper")
            if not 0 <= lower < upper <= 1:
                raise FactorExpressionError("winsorization bounds are invalid")
            normalized.update(lower=lower, upper=upper)
        if set(raw_params) - expected:
            raise FactorExpressionError(f"operator {name} contains unknown params")
        return normalized

    @staticmethod
    def _number(value: Any, label: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise FactorExpressionError(f"{label} must be numeric")
        result = float(value)
        if not math.isfinite(result):
            raise FactorExpressionError(f"{label} must be finite")
        return result

    @staticmethod
    def _integer(value: Any, label: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise FactorExpressionError(f"{label} must be an integer")
        return value

    def as_dict(self) -> dict[str, Any]:
        if self.kind == "field":
            return {"field": self.name}
        if self.kind == "constant":
            return {"constant": self.value}
        payload: dict[str, Any] = {
            "op": self.name,
            "args": [item.as_dict() for item in self.args],
        }
        if self.params:
            payload["params"] = dict(self.params)
        return payload

    @property
    def fields(self) -> frozenset[str]:
        if self.kind == "field":
            return frozenset({str(self.name)})
        return frozenset().union(*(item.fields for item in self.args))

    @property
    def lookback(self) -> int:
        if self.kind == "field":
            return 1
        if self.kind == "constant":
            return 0
        base = max(item.lookback for item in self.args)
        params = dict(self.params)
        if self.name in PERIOD_OPERATORS:
            return base + int(params["periods"])
        if self.name in ROLLING_OPERATORS | PAIR_ROLLING_OPERATORS | {"ts_quantile"}:
            return base + int(params["window"]) - 1
        return base

    def evaluate(self, frame: pd.DataFrame) -> pd.Series | float:
        if self.kind == "field":
            return pd.to_numeric(frame[str(self.name)], errors="coerce").astype(float)
        if self.kind == "constant":
            return float(self.value)
        values = [item.evaluate(frame) for item in self.args]
        params = dict(self.params)
        name = str(self.name)
        if name in BINARY_OPERATORS:
            return _binary(name, values[0], values[1])
        series = _series(values[0], frame.index)
        if name == "abs":
            return op.abs_value(series)
        if name == "inverse":
            return op.inverse(series)
        if name == "log":
            return op.log(series)
        if name in {"rank", "demean", "scale"}:
            return getattr(op, name)(series)
        if name in PERIOD_OPERATORS:
            return getattr(op, name)(series, int(params["periods"]))
        if name in ROLLING_OPERATORS:
            return getattr(op, name)(series, int(params["window"]), int(params["min_periods"]))
        if name in PAIR_ROLLING_OPERATORS:
            return getattr(op, name)(
                series,
                _series(values[1], frame.index),
                int(params["window"]),
                int(params["min_periods"]),
            )
        if name == "signed_power":
            return op.signed_power(series, float(params["exponent"]))
        if name == "ts_quantile":
            return op.ts_quantile(
                series,
                int(params["window"]),
                float(params["quantile"]),
                int(params["min_periods"]),
            )
        if name == "winsorize_zscore":
            return op.winsorize_zscore(
                series, float(params["lower"]), float(params["upper"])
            )
        raise FactorExpressionError(f"operator has no interpreter: {name}")


def _series(value: pd.Series | float, index: pd.Index) -> pd.Series:
    if isinstance(value, pd.Series):
        return value
    return pd.Series(float(value), index=index, dtype=float)


def _binary(
    name: str,
    left: pd.Series | float,
    right: pd.Series | float,
) -> pd.Series:
    index = left.index if isinstance(left, pd.Series) else right.index
    lhs, rhs = _series(left, index), _series(right, index)
    if name == "add":
        return op.add(lhs, rhs)
    if name == "sub":
        return op.sub(lhs, rhs)
    if name == "mul":
        return op.mul(lhs, rhs)
    return op.div(lhs, rhs)


@dataclass(frozen=True)
class FactorExpressionSpec:
    name: str
    hypothesis: str
    direction: int
    role: str
    expression: ExpressionNode
    source: str = "human"
    parent_hash: str | None = None
    protocol_version: str = EXPRESSION_PROTOCOL_VERSION

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "FactorExpressionSpec":
        if not isinstance(payload, Mapping):
            raise FactorExpressionError("factor expression spec must be an object")
        allowed = {
            "name", "hypothesis", "direction", "role", "expression", "source",
            "parent_hash", "protocol_version",
        }
        if set(payload) - allowed:
            raise FactorExpressionError("factor expression spec contains unknown keys")
        name = str(payload.get("name", ""))
        if not _SLUG.fullmatch(name):
            raise FactorExpressionError("factor name must be a lowercase underscore slug")
        hypothesis = str(payload.get("hypothesis", "")).strip()
        if not hypothesis or len(hypothesis) > 1000:
            raise FactorExpressionError("factor hypothesis is required and limited to 1000 characters")
        direction = payload.get("direction")
        if direction not in {-1, 1}:
            raise FactorExpressionError("factor direction must be -1 or 1")
        role = payload.get("role")
        if role not in {"rank", "filter", "risk"}:
            raise FactorExpressionError("factor role must be rank, filter or risk")
        version = str(payload.get("protocol_version", EXPRESSION_PROTOCOL_VERSION))
        if version != EXPRESSION_PROTOCOL_VERSION:
            raise FactorExpressionError(f"unsupported factor expression protocol: {version}")
        source = str(payload.get("source", "human")).strip()
        if not source or len(source) > 120:
            raise FactorExpressionError("factor source is required and limited to 120 characters")
        parent_hash = payload.get("parent_hash")
        if parent_hash is not None and not re.fullmatch(r"[0-9a-f]{64}", str(parent_hash)):
            raise FactorExpressionError("parent_hash must be a SHA-256 hex digest")
        node = ExpressionNode.from_dict(payload.get("expression", {}))
        if not node.fields:
            raise FactorExpressionError("factor expression must depend on market fields")
        return cls(
            name=name,
            hypothesis=hypothesis,
            direction=int(direction),
            role=str(role),
            expression=node,
            source=source,
            parent_hash=str(parent_hash) if parent_hash else None,
            protocol_version=version,
        )

    @property
    def expression_hash(self) -> str:
        canonical = json.dumps(
            {
                "protocol_version": self.protocol_version,
                "expression": self.expression.as_dict(),
            },
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    @property
    def required_fields(self) -> tuple[str, ...]:
        return tuple(sorted(self.expression.fields))

    @property
    def lookback(self) -> int:
        return self.expression.lookback

    def as_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "name": self.name,
            "hypothesis": self.hypothesis,
            "direction": self.direction,
            "role": self.role,
            "source": self.source,
            "parent_hash": self.parent_hash,
            "expression": self.expression.as_dict(),
            "expression_hash": self.expression_hash,
            "required_fields": list(self.required_fields),
            "lookback": self.lookback,
        }

    def compute(self, frame: pd.DataFrame) -> pd.Series:
        missing = sorted(set(self.required_fields) - set(frame.columns))
        if missing:
            raise FactorExpressionError(f"factor data is missing fields: {', '.join(missing)}")
        result = self.expression.evaluate(frame)
        if not isinstance(result, pd.Series):
            raise FactorExpressionError("factor expression did not produce a panel series")
        result = pd.to_numeric(result, errors="coerce").replace([np.inf, -np.inf], np.nan)
        if not result.index.equals(frame.index):
            raise FactorExpressionError("factor result is not aligned to its input panel")
        return result.rename(self.name)
