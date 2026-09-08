"""Shared point-in-time selection helpers for A-share research candidates."""
from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from quant_engine.backtest.protocol import StrategyOutput

MIN_HISTORY_BARS = 120
ELIGIBILITY_WINDOW = 252
RECENT_WINDOW = 20
MIN_RECENT_BARS = 15
MAX_POSITION_WEIGHT = 0.10


@dataclass(frozen=True)
class EligibleHistory:
    """One security's ordered history after shared eligibility checks."""

    code: str
    frame: pd.DataFrame
    signal_close: pd.Series


def requested_history_bars(signal_lookback: int) -> int:
    """Include the annual eligibility window and both signal endpoints."""
    return max(ELIGIBILITY_WINDOW, signal_lookback + 1)


def _numeric(values: pd.Series) -> pd.Series:
    return pd.to_numeric(values, errors="coerce").astype(float)


def _is_as_of(value: object, as_of: date) -> bool:
    try:
        return pd.Timestamp(value).date() == as_of
    except (TypeError, ValueError):
        return False


def eligible_histories(
    history: pd.DataFrame,
    *,
    as_of: date,
    min_amount: float,
) -> dict[str, EligibleHistory]:
    """Apply common age, tradability, recency and liquidity requirements.

    Suspended rows remain in the raw window, but their closes are set to NaN
    for every signal calculation.  This prevents a carried valuation price
    from becoming a zero return in the strategy signal.
    """
    required = {"close", "volume", "amount"}
    if history.empty or not required.issubset(history.columns):
        return {}
    if not isinstance(history.index, pd.MultiIndex) or "code" not in history.index.names:
        return {}

    result: dict[str, EligibleHistory] = {}
    codes = sorted({str(value) for value in history.index.get_level_values("code")})
    for code in codes:
        try:
            frame = history.xs(code, level="code").sort_index().copy()
        except (KeyError, TypeError, ValueError):
            continue
        if frame.empty or frame.index.has_duplicates or not _is_as_of(frame.index[-1], as_of):
            continue

        close = _numeric(frame["close"])
        volume = _numeric(frame["volume"])
        amount = _numeric(frame["amount"])
        tradable = (
            np.isfinite(close)
            & (close > 0)
            & np.isfinite(volume)
            & (volume > 0)
        )
        signal_close = close.where(tradable)
        eligibility_tradable = tradable.tail(ELIGIBILITY_WINDOW)
        if not bool(tradable.iloc[-1]) or int(eligibility_tradable.sum()) < MIN_HISTORY_BARS:
            continue

        recent_tradable = tradable.tail(RECENT_WINDOW)
        if int(recent_tradable.sum()) < MIN_RECENT_BARS:
            continue
        recent_amount = amount.tail(RECENT_WINDOW).where(recent_tradable)
        recent_amount = recent_amount.where(np.isfinite(recent_amount) & (recent_amount >= 0))
        if int(recent_amount.notna().sum()) < MIN_RECENT_BARS:
            continue
        average_amount = float(recent_amount.mean())
        if not math.isfinite(average_amount) or average_amount < min_amount:
            continue

        result[code] = EligibleHistory(code=code, frame=frame, signal_close=signal_close)
    return result


def score_low_volatility(item: EligibleHistory, lookback: int) -> float | None:
    """Return trailing daily-return volatility, without filling missing rows."""
    window = item.signal_close.tail(lookback + 1)
    if len(window) != lookback + 1:
        return None
    returns = window.pct_change(fill_method=None).replace([np.inf, -np.inf], np.nan)
    if int(returns.notna().sum()) < math.ceil(lookback * 0.80):
        return None
    score = float(returns.std())
    return score if math.isfinite(score) else None


def score_trailing_return(item: EligibleHistory, lookback: int) -> float | None:
    """Return the point-to-point trailing return when both endpoints are valid."""
    window = item.signal_close.tail(lookback + 1)
    if len(window) != lookback + 1:
        return None
    start, end = float(window.iloc[0]), float(window.iloc[-1])
    if not math.isfinite(start) or not math.isfinite(end) or start <= 0 or end <= 0:
        return None
    score = end / start - 1.0
    return score if math.isfinite(score) else None


def score_skipped_momentum(
    item: EligibleHistory,
    lookback: int,
    skip: int,
) -> float | None:
    """Return momentum from ``t-lookback`` through ``t-skip``."""
    window = item.signal_close.tail(lookback + 1)
    if len(window) != lookback + 1:
        return None
    start, end = float(window.iloc[0]), float(window.iloc[-1 - skip])
    if not math.isfinite(start) or not math.isfinite(end) or start <= 0 or end <= 0:
        return None
    score = end / start - 1.0
    return score if math.isfinite(score) else None


def build_selection_output(
    eligible: Mapping[str, EligibleHistory],
    *,
    scorer: Callable[[EligibleHistory], float | None],
    top_n: int,
    gross_exposure: float,
    ascending: bool,
) -> StrategyOutput:
    """Score, deterministically rank and equal-weight eligible securities."""
    scores: list[tuple[str, float]] = []
    for code in sorted(eligible):
        score = scorer(eligible[code])
        if score is not None and math.isfinite(score):
            scores.append((code, float(score)))
    scores.sort(key=(lambda item: (item[1], item[0])) if ascending else (lambda item: (-item[1], item[0])))
    selected = scores[:top_n]
    if selected and gross_exposure > 0:
        weight = min(MAX_POSITION_WEIGHT, gross_exposure / len(selected))
        targets = {code: weight for code, _ in selected}
    else:
        targets = {}
    gross = float(sum(targets.values()))
    return StrategyOutput(targets, {
        "eligible_count": len(scores),
        "selected_count": len(targets),
        "gross_exposure": gross,
    })
