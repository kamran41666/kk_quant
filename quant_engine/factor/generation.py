"""Candidate generator interface and deterministic built-in adapter."""
from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from quant_engine.factor.expression import FactorExpressionSpec


@runtime_checkable
class FactorCandidateGenerator(Protocol):
    """Small seam shared by deterministic and future model adapters."""

    name: str

    def generate(
        self,
        *,
        memory: Mapping[str, Any],
        limit: int,
    ) -> Sequence[FactorExpressionSpec]: ...


def _zscore(expression: dict[str, Any]) -> dict[str, Any]:
    return {
        "op": "winsorize_zscore",
        "args": [expression],
        "params": {"lower": 0.01, "upper": 0.99},
    }


def _lagged_return(periods: int) -> dict[str, Any]:
    return {
        "op": "sub",
        "args": [
            {
                "op": "div",
                "args": [
                    {"op": "delay", "args": [{"field": "close"}], "params": {"periods": 1}},
                    {"op": "delay", "args": [{"field": "close"}], "params": {"periods": periods + 1}},
                ],
            },
            {"constant": 1.0},
        ],
    }


TEMPLATE_CANDIDATES: tuple[dict[str, Any], ...] = (
    {
        "name": "template_short_reversal_5",
        "hypothesis": "上一完整交易周涨幅过高的股票可能出现短期均值回归。",
        "direction": -1,
        "role": "rank",
        "source": "template-generator-v1",
        "expression": _zscore(_lagged_return(5)),
    },
    {
        "name": "template_low_volatility_20",
        "hypothesis": "过去完整20日收益波动较低的股票可能获得更稳健的风险调整收益。",
        "direction": -1,
        "role": "rank",
        "source": "template-generator-v1",
        "expression": _zscore({
            "op": "ts_std",
            "args": [_lagged_return(1)],
            "params": {"window": 20, "min_periods": 12},
        }),
    },
    {
        "name": "template_volume_price_divergence_10",
        "hypothesis": "价格收益与成交量扩张持续同向时可能代表短期拥挤，后续收益倾向回落。",
        "direction": -1,
        "role": "rank",
        "source": "template-generator-v1",
        "expression": _zscore({
            "op": "ts_corr",
            "args": [
                _lagged_return(1),
                {
                    "op": "sub",
                    "args": [
                        {"op": "log", "args": [{"field": "volume"}]},
                        {
                            "op": "log",
                            "args": [{
                                "op": "delay",
                                "args": [{"field": "volume"}],
                                "params": {"periods": 2},
                            }],
                        },
                    ],
                },
            ],
            "params": {"window": 10, "min_periods": 6},
        }),
    },
    {
        "name": "template_range_compression_20",
        "hypothesis": "近期振幅相对中期振幅收缩可能对应波动压缩状态。",
        "direction": -1,
        "role": "filter",
        "source": "template-generator-v1",
        "expression": _zscore({
            "op": "div",
            "args": [
                {
                    "op": "ts_mean",
                    "args": [{
                        "op": "div",
                        "args": [
                            {"op": "sub", "args": [{"field": "high"}, {"field": "low"}]},
                            {"field": "close"},
                        ],
                    }],
                    "params": {"window": 5, "min_periods": 3},
                },
                {
                    "op": "ts_mean",
                    "args": [{
                        "op": "div",
                        "args": [
                            {"op": "sub", "args": [{"field": "high"}, {"field": "low"}]},
                            {"field": "close"},
                        ],
                    }],
                    "params": {"window": 20, "min_periods": 12},
                },
            ],
        }),
    },
)


class TemplateCandidateGenerator:
    name = "template-v1"

    def generate(
        self,
        *,
        memory: Mapping[str, Any],
        limit: int,
    ) -> Sequence[FactorExpressionSpec]:
        if not 1 <= limit <= 50:
            raise ValueError("candidate generation limit must be in [1, 50]")
        seen = {
            str(item.get("expression_hash")) for item in memory.get("items", [])
            if item.get("expression_hash")
        }
        result = []
        for payload in TEMPLATE_CANDIDATES:
            spec = FactorExpressionSpec.from_dict(payload)
            if spec.expression_hash in seen:
                continue
            result.append(spec)
            if len(result) >= limit:
                break
        return tuple(result)


GENERATORS: dict[str, FactorCandidateGenerator] = {
    TemplateCandidateGenerator.name: TemplateCandidateGenerator(),
}
