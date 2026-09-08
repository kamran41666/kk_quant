"""Evidence-oriented workflow for generating and screening factor candidates.

Feature generation is deliberately separate from forward-return labels.  The
caller must supply labels produced by its frozen research protocol; this keeps
future observations out of every deployable factor function.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

from quant_engine.factor.catalog import compute_factor, list_factor_definitions
from quant_engine.factor.evaluation import (
    calc_ic,
    calc_ic_summary,
    factor_correlation_matrix,
)
from quant_engine.factor.operators import to_wide

DEFAULT_ADVANCED_FACTORS = tuple(
    item.name for item in list_factor_definitions("advanced")
)


@dataclass(frozen=True)
class CandidateEvidence:
    name: str
    accepted: bool
    decision_reason: str
    maximum_absolute_correlation: float | None
    most_correlated_factor: str | None
    ic_summary: Mapping[str, float] | None


@dataclass(frozen=True)
class FactorResearchResult:
    """Values and measured gates; acceptance is not a profitability claim."""

    values: Mapping[str, pd.DataFrame]
    accepted: tuple[str, ...]
    evidence: tuple[CandidateEvidence, ...]
    correlation_matrix: pd.DataFrame


def _as_wide(values: pd.Series | pd.DataFrame) -> pd.DataFrame:
    if isinstance(values, pd.Series):
        return to_wide(values)
    return values.apply(pd.to_numeric, errors="coerce").replace([np.inf, -np.inf], np.nan)


def run_factor_research(
    data: pd.DataFrame,
    *,
    candidate_names: Sequence[str] = DEFAULT_ADVANCED_FACTORS,
    existing_factors: Mapping[str, pd.Series | pd.DataFrame] | None = None,
    forward_returns: pd.Series | pd.DataFrame | None = None,
    correlation_limit: float = 0.70,
    ic_method: str = "rank",
) -> FactorResearchResult:
    """Generate, measure and greedily de-correlate candidate factors.

    Candidates are considered in the caller-provided order.  Each candidate
    must be below ``correlation_limit`` against both the supplied existing
    factors and already accepted candidates.  IC is reported when an explicit
    forward-return label matrix is provided, but no minimum IC is invented by
    this module.
    """
    if not 0 < correlation_limit < 1:
        raise ValueError("correlation_limit must be between zero and one")
    names = tuple(dict.fromkeys(candidate_names))
    generated = {name: _as_wide(compute_factor(name, data)) for name in names}
    baselines = {
        name: _as_wide(values) for name, values in (existing_factors or {}).items()
    }
    all_values = {**baselines, **generated}
    with np.errstate(divide="ignore", invalid="ignore"):
        correlations = factor_correlation_matrix(all_values)
    label_matrix = _as_wide(forward_returns) if forward_returns is not None else None

    accepted: list[str] = []
    evidence: list[CandidateEvidence] = []
    baseline_names = list(baselines)
    for name in names:
        comparisons = baseline_names + accepted
        finite_correlations: dict[str, float] = {}
        if comparisons and name in correlations.index:
            row = correlations.loc[name, comparisons].abs().replace([np.inf, -np.inf], np.nan).dropna()
            finite_correlations = {str(key): float(value) for key, value in row.items()}
        closest = max(finite_correlations, key=finite_correlations.get) if finite_correlations else None
        maximum = finite_correlations.get(closest) if closest is not None else None
        has_values = bool(np.isfinite(generated[name].to_numpy(dtype=float)).any())
        if not has_values or (comparisons and not finite_correlations):
            passed = False
            decision_reason = "insufficient_evidence"
        elif maximum is not None and maximum > correlation_limit:
            passed = False
            decision_reason = "correlation_limit_exceeded"
        else:
            passed = True
            decision_reason = "correlation_gate_passed"
        if passed:
            accepted.append(name)
        summary = None
        if label_matrix is not None:
            summary = calc_ic_summary(calc_ic(generated[name], label_matrix, method=ic_method))
        evidence.append(CandidateEvidence(
            name=name,
            accepted=passed,
            decision_reason=decision_reason,
            maximum_absolute_correlation=maximum,
            most_correlated_factor=closest,
            ic_summary=summary,
        ))
    return FactorResearchResult(
        values=generated,
        accepted=tuple(accepted),
        evidence=tuple(evidence),
        correlation_matrix=correlations,
    )
