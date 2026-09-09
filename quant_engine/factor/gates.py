"""Explicit training-only gates for restricted factor experiments."""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping


class FactorGateError(ValueError):
    pass


@dataclass(frozen=True)
class FactorGatePolicy:
    min_coverage: float = 0.70
    min_ic_observations: int = 60
    min_directional_ic: float = 0.02
    max_absolute_correlation: float = 0.70
    require_half_sign_consistency: bool = True
    min_directional_quantile_spread: float = 0.0

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None = None) -> "FactorGatePolicy":
        raw = dict(payload or {})
        allowed = set(cls.__dataclass_fields__)
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise FactorGateError(f"unknown factor gate policy fields: {', '.join(unknown)}")
        try:
            policy = cls(**raw)
        except TypeError as exc:
            raise FactorGateError(f"invalid factor gate policy: {exc}") from exc
        numeric = {
            "min_coverage": policy.min_coverage,
            "min_directional_ic": policy.min_directional_ic,
            "max_absolute_correlation": policy.max_absolute_correlation,
            "min_directional_quantile_spread": policy.min_directional_quantile_spread,
        }
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in numeric.values()):
            raise FactorGateError("factor gate numeric thresholds must be numbers")
        if not all(math.isfinite(float(value)) for value in numeric.values()):
            raise FactorGateError("factor gate thresholds must be finite")
        if not 0 <= policy.min_coverage <= 1:
            raise FactorGateError("min_coverage must be in [0, 1]")
        if not 0 <= policy.min_directional_ic <= 1:
            raise FactorGateError("min_directional_ic must be in [0, 1]")
        if not 0 < policy.max_absolute_correlation < 1:
            raise FactorGateError("max_absolute_correlation must be in (0, 1)")
        if isinstance(policy.min_ic_observations, bool) or not isinstance(policy.min_ic_observations, int):
            raise FactorGateError("min_ic_observations must be an integer")
        if not 3 <= policy.min_ic_observations <= 10_000:
            raise FactorGateError("min_ic_observations must be in [3, 10000]")
        if not isinstance(policy.require_half_sign_consistency, bool):
            raise FactorGateError("require_half_sign_consistency must be boolean")
        return policy

    def as_dict(self) -> dict[str, Any]:
        return {
            key: getattr(self, key) for key in self.__dataclass_fields__
        }


def apply_training_gates(
    policy: FactorGatePolicy,
    *,
    coverage: float,
    ic_observations: int,
    directional_ic: float | None,
    maximum_absolute_correlation: float | None,
    half_sign_consistent: bool | None,
    directional_quantile_spread: float | None,
) -> tuple[str, list[dict[str, Any]]]:
    """Return a training decision plus independently auditable gate results."""
    gates = [
        {
            "name": "coverage",
            "passed": coverage >= policy.min_coverage,
            "actual": coverage,
            "rule": f">={policy.min_coverage}",
        },
        {
            "name": "ic_observations",
            "passed": ic_observations >= policy.min_ic_observations,
            "actual": ic_observations,
            "rule": f">={policy.min_ic_observations}",
        },
        {
            "name": "directional_ic",
            "passed": directional_ic is not None and directional_ic >= policy.min_directional_ic,
            "actual": directional_ic,
            "rule": f">={policy.min_directional_ic}",
        },
        {
            "name": "baseline_correlation",
            "passed": maximum_absolute_correlation is not None
            and maximum_absolute_correlation <= policy.max_absolute_correlation,
            "actual": maximum_absolute_correlation,
            "rule": f"<={policy.max_absolute_correlation}",
        },
        {
            "name": "half_sign_consistency",
            "passed": (
                half_sign_consistent is True
                if policy.require_half_sign_consistency
                else half_sign_consistent is not None
            ),
            "actual": half_sign_consistent,
            "rule": "required" if policy.require_half_sign_consistency else "evidence_required",
        },
        {
            "name": "directional_quantile_spread",
            "passed": directional_quantile_spread is not None
            and directional_quantile_spread >= policy.min_directional_quantile_spread,
            "actual": directional_quantile_spread,
            "rule": f">={policy.min_directional_quantile_spread}",
        },
    ]
    decision = "training_passed" if all(item["passed"] for item in gates) else "training_rejected"
    return decision, gates
