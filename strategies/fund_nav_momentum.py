"""Beginner-friendly domestic fund NAV strategy.

This is intentionally a small, deterministic strategy for the first fund
observation slice.  It ranks the supplied funds by trailing NAV return and
holds the strongest positive fund(s); it never invents an intraday price.
"""
from __future__ import annotations

from datetime import date
from typing import Mapping, Sequence

from quant_engine.backtest.strategy import Strategy


class FundNavMomentumStrategy(Strategy):
    """Daily/weekly positive-momentum allocation over a supplied fund pool."""

    def initialize(self):
        self.lookback = max(2, int(self._strategy_kwargs.get("lookback", 20)))
        self.top_n = max(1, int(self._strategy_kwargs.get("top_n", 1)))
        self.target_weight = min(1.0, max(0.0, float(self._strategy_kwargs.get("target_weight", 1.0))))
        self._fund_history: Mapping[str, Sequence[Mapping[str, object]]] = {}

    def generate_signals(self, dt: date) -> dict[str, float]:
        history = self._fund_history
        scores: list[tuple[str, float]] = []
        for code, rows in history.items():
            usable = [row for row in rows if str(row.get("date")) <= dt.isoformat()]
            if len(usable) < self.lookback + 1:
                continue
            try:
                latest = float(usable[-1].get("nav"))
                base = float(usable[-1 - self.lookback].get("nav"))
            except (TypeError, ValueError):
                continue
            if latest > 0 and base > 0:
                scores.append((str(code), latest / base - 1.0))
        positive = sorted((item for item in scores if item[1] > 0), key=lambda item: item[1], reverse=True)
        selected = positive[: self.top_n]
        if not selected or self.target_weight <= 0:
            return {}
        weight = self.target_weight / len(selected)
        return {code: weight for code, _ in selected}
