"""Deterministic daily COPA_PRICE_ONLY strategy for A-share research.

The public Cycle of Price Action material is discretionary.  This module is a
frozen price/volume hypothesis, not a reproduction of Oliver Kell's trades or
performance.  State is replayed from point-in-time history on every call so a
backtest process and a newly-created paper-observation process agree.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from quant_engine.backtest.protocol import (
    AnalysisOutputSpec,
    DataRequirement,
    ParameterSpec,
    ParameterType,
    StrategyOutput,
    StrategySpec,
)
from quant_engine.backtest.strategy import Strategy


@dataclass(frozen=True)
class _ActiveSignal:
    code: str
    signal: str
    entry_close: float
    stop: float
    pivot: float
    entry_index: int
    exhausted: bool
    score: float


class CycleOfPriceActionStrategy(Strategy):
    """Replayable S1-S5 price-action state machine with target-risk sizing."""

    SPEC = StrategySpec(
        id="cycle-of-price-action-price-only",
        name="Cycle of Price Action（价格量能版）",
        version="1.0.0",
        description="市场宽度、领导强度、Wedge Pop、首次 EMA Crossback、Base Break 和趋势退出的日线纯多头研究策略。",
        markets=("a-share",),
        parameters=(
            ParameterSpec("ablation_stage", "消融阶段", ParameterType.STRING, "S5", choices=("S1", "S2", "S3", "S4", "S5")),
            ParameterSpec("history_bars", "历史窗口", ParameterType.INTEGER, 300, minimum=280, maximum=420),
            ParameterSpec("max_positions", "最大持仓数", ParameterType.INTEGER, 10, minimum=8, maximum=12),
            ParameterSpec("leader_quantile", "领导强度分位", ParameterType.NUMBER, 0.70, minimum=0.50, maximum=0.90),
            ParameterSpec("min_rvol", "突破最小量比", ParameterType.NUMBER, 1.20, minimum=1.0, maximum=2.0),
            ParameterSpec("min_avg_amount", "最低20日均成交额", ParameterType.NUMBER, 20_000_000.0, minimum=0.0, maximum=10_000_000_000.0),
            ParameterSpec("risk_per_trade", "目标单笔风险", ParameterType.NUMBER, 0.0025, minimum=0.0005, maximum=0.01),
            ParameterSpec("max_position_weight", "单票仓位上限", ParameterType.NUMBER, 0.15, minimum=0.05, maximum=0.20),
            ParameterSpec("initial_position_fraction", "首批仓位比例", ParameterType.NUMBER, 0.50, minimum=0.25, maximum=1.0, advanced=True),
            ParameterSpec("risk_on_breadth", "Risk-on宽度", ParameterType.NUMBER, 0.55, minimum=0.45, maximum=0.70),
            ParameterSpec("risk_off_breadth", "Risk-off宽度", ParameterType.NUMBER, 0.40, minimum=0.20, maximum=0.50),
            ParameterSpec("risk_on_exposure", "Risk-on总仓位", ParameterType.NUMBER, 0.90, minimum=0.50, maximum=0.90),
            ParameterSpec("neutral_exposure", "Neutral总仓位", ParameterType.NUMBER, 0.50, minimum=0.10, maximum=0.70),
            ParameterSpec("risk_off_exposure", "Risk-off总仓位", ParameterType.NUMBER, 0.00, minimum=0.0, maximum=0.20),
            ParameterSpec("cooldown_bars", "退出冷却日", ParameterType.INTEGER, 3, minimum=0, maximum=10, advanced=True),
        ),
        data=(DataRequirement(
            "a_share_daily",
            ("open", "high", "low", "close", "volume", "amount"),
            280,
            lookback_parameter="history_bars",
        ),),
        rebalance_frequency="daily",
        warmup_bars=280,
        max_gross_exposure=0.90,
        tags=("趋势", "突破", "状态重放", "价格量能", "实验策略"),
        analysis_outputs=(
            AnalysisOutputSpec("market_regime", "市场状态", "string"),
            AnalysisOutputSpec("breadth", "市场宽度", "number"),
            AnalysisOutputSpec("eligible_count", "有效样本数", "integer"),
            AnalysisOutputSpec("selected_count", "目标持仓数", "integer"),
            AnalysisOutputSpec("wedge_pop_count", "Wedge Pop数", "integer"),
            AnalysisOutputSpec("crossback_count", "首次Crossback数", "integer"),
            AnalysisOutputSpec("base_break_count", "Base Break数", "integer"),
            AnalysisOutputSpec("exit_count", "当日退出数", "integer"),
            AnalysisOutputSpec("exhaustion_count", "延伸减仓数", "integer"),
            AnalysisOutputSpec("gross_target", "目标总仓位", "number"),
            AnalysisOutputSpec("selected_signals", "入选信号", "json"),
        ),
        research_document="docs/strategy-library/cycle-of-price-action.md",
        extensions={
            "research_status": "unvalidated",
            "scope": "COPA_PRICE_ONLY_S1_S5_DAILY_CLOSE",
            "limitations": [
                "universe breadth and cross-sectional return percentile proxy for index regime and benchmark RS",
                "growth fundamentals, ST/listing metadata and industry caps are not available",
                "risk sizing uses signal-close stop distance rather than the unknown next-open fill risk",
                "close-confirmed targets execute at the next trading-day open; intraday trigger orders are excluded",
            ],
        },
    )

    def initialize(self) -> None:
        for key, value in self.params.items():
            setattr(self, key, value)
        if self.risk_off_breadth >= self.risk_on_breadth:
            raise ValueError("risk_off_breadth must be lower than risk_on_breadth")
        if self.neutral_exposure > self.risk_on_exposure:
            raise ValueError("neutral_exposure cannot exceed risk_on_exposure")
        if self.risk_off_exposure > self.neutral_exposure:
            raise ValueError("risk_off_exposure cannot exceed neutral_exposure")

    @staticmethod
    def _is_main_board(code: str) -> bool:
        if code.endswith(".SH"):
            return code.startswith(("600", "601", "603", "605"))
        if code.endswith(".SZ"):
            return code.startswith(("000", "001", "002", "003"))
        return False

    @staticmethod
    def _atr(frame: pd.DataFrame) -> pd.Series:
        previous_close = frame["close"].shift(1)
        true_range = pd.concat(
            (
                frame["high"] - frame["low"],
                (frame["high"] - previous_close).abs(),
                (frame["low"] - previous_close).abs(),
            ),
            axis=1,
        ).max(axis=1)
        return true_range.ewm(alpha=1.0 / 14.0, adjust=False, min_periods=14).mean()

    def _features(self, frame: pd.DataFrame) -> pd.DataFrame:
        values = frame.sort_index().tail(self.history_bars).copy()
        for field in ("open", "high", "low", "close", "volume", "amount"):
            values[field] = pd.to_numeric(values[field], errors="coerce")
        close = values["close"]
        high = values["high"]
        low = values["low"]
        ema10 = close.ewm(span=10, adjust=False, min_periods=10).mean()
        ema20 = close.ewm(span=20, adjust=False, min_periods=20).mean()
        atr = self._atr(values)
        prior_volume = values["volume"].shift(1).rolling(20).median()
        result = pd.DataFrame(index=values.index)
        result[["open", "high", "low", "close"]] = values[["open", "high", "low", "close"]]
        result["ema10"] = ema10
        result["ema20"] = ema20
        result["sma50"] = close.rolling(50).mean()
        result["sma200"] = close.rolling(200).mean()
        result["atr"] = atr
        result["rvol"] = values["volume"] / prior_volume.replace(0, np.nan)
        result["avg_amount20"] = values["amount"].rolling(20).mean()
        result["return20"] = close.pct_change(20)
        result["return63"] = close.pct_change(63)
        result["ema20_slope5"] = ema20.pct_change(5)
        result["extension"] = (close - ema10) / atr.replace(0, np.nan)
        result["prior_high10"] = high.shift(1).rolling(10).max()
        result["base_low10"] = low.shift(1).rolling(10).min()
        result["reset_seen"] = (close < ema20).shift(1).rolling(60).max().fillna(0).astype(bool)
        result["tight5"] = (
            high.shift(1).rolling(5).max() - low.shift(1).rolling(5).min() <= 1.5 * atr.shift(1)
        ) & ((ema10.shift(1) - ema20.shift(1)).abs() / close.shift(1) <= 0.01)
        first_range = high.shift(6).rolling(5).max() - low.shift(6).rolling(5).min()
        second_range = high.shift(1).rolling(5).max() - low.shift(1).rolling(5).min()
        result["base_contracting"] = second_range <= first_range
        result["base_width_ok"] = (
            high.shift(1).rolling(10).max() - low.shift(1).rolling(10).min() <= 3.0 * atr.shift(1)
        )
        result["long_trend"] = (
            (close > ema10)
            & (ema10 > ema20)
            & (result["ema20_slope5"] > 0)
            & (close > result["sma50"])
        )
        result["upper_half"] = (
            (close - low) / (high - low).clip(lower=close.abs() * 1e-6) >= 0.5
        )
        result["weak_exit"] = (
            ((close < ema20 - 0.25 * atr) & (result["rvol"] >= self.min_rvol))
            | ((close < ema20).rolling(2).sum() >= 2)
        )
        result["wedge_drop"] = (
            (close < low.shift(1).rolling(5).min())
            & (close < ema20)
            & (result["rvol"] >= self.min_rvol)
        )
        result["exhaustion"] = (
            (result["extension"] >= 2.5)
            & (close < values["open"])
            & (result["rvol"] >= self.min_rvol)
        )
        return result.replace([np.inf, -np.inf], np.nan)

    @staticmethod
    def _confirmed_regimes(breadth: pd.Series, risk_on: float, risk_off: float) -> pd.Series:
        raw = pd.Series("neutral", index=breadth.index, dtype="object")
        raw.loc[breadth >= risk_on] = "risk-on"
        raw.loc[breadth <= risk_off] = "risk-off"
        confirmed: list[str] = []
        current = "neutral"
        previous_raw: str | None = None
        streak = 0
        for value in raw:
            streak = streak + 1 if value == previous_raw else 1
            previous_raw = str(value)
            if streak >= 2:
                current = str(value)
            confirmed.append(current)
        return pd.Series(confirmed, index=breadth.index, dtype="object")

    def _mark_events(self, frame: pd.DataFrame) -> pd.DataFrame:
        result = frame.copy()
        leader = result["leader"].fillna(False)
        allowed = result["regime"].ne("risk-off")
        result["wedge_pop"] = (
            result["reset_seen"]
            & result["tight5"]
            & (result["close"] > result["prior_high10"])
            & (result["close"] > result["ema10"])
            & (result["close"] > result["ema20"])
            & (result["rvol"] >= self.min_rvol)
            & leader
            & allowed
        )
        result["base_break"] = (
            result["long_trend"]
            & result["base_contracting"]
            & result["base_width_ok"]
            & (result["base_low10"] >= result["ema20"] - 0.5 * result["atr"])
            & (result["close"] > result["prior_high10"])
            & (result["rvol"] >= self.min_rvol)
            & (result["extension"] <= 1.5)
            & leader
            & allowed
        )
        crossbacks: list[bool] = []
        last_wedge = -10_000
        used = False
        for index, (_, row) in enumerate(result.iterrows()):
            if bool(row["wedge_pop"]):
                last_wedge = index
                used = False
            crossback = (
                0 < index - last_wedge <= 20
                and not used
                and bool(row["long_trend"])
                and float(row["low"]) <= max(float(row["ema10"]), float(row["ema20"])) + 0.25 * float(row["atr"])
                and float(row["close"]) >= float(row["ema20"])
                and bool(row["upper_half"])
                and float(row["rvol"]) <= 1.2
                and bool(leader.iloc[index])
                and bool(allowed.iloc[index])
            )
            crossbacks.append(bool(crossback))
            if crossback:
                used = True
        result["crossback"] = crossbacks
        return result

    def _replay(self, code: str, frame: pd.DataFrame) -> tuple[_ActiveSignal | None, bool]:
        active: _ActiveSignal | None = None
        cooldown_until = -1
        exit_today = False
        stage = int(self.ablation_stage[1:])
        for index, (_, row) in enumerate(frame.iterrows()):
            finite = all(math.isfinite(float(row[field])) for field in ("close", "atr", "ema20", "return63"))
            if not finite:
                continue
            close = float(row["close"])
            if active is not None:
                post_signal_highs = frame["high"].iloc[active.entry_index + 1:index + 1]
                maximum = float(post_signal_highs.max()) if not post_signal_highs.empty else active.entry_close
                risk = max(0.0, active.entry_close - active.stop)
                failed = (
                    active.signal in {"wedge-pop", "base-break"}
                    and index - active.entry_index <= 3
                    and risk > 0
                    and maximum - active.entry_close < 0.5 * risk
                    and close < active.pivot
                )
                stop_breached = close < active.stop
                regime_exit = row["regime"] == "risk-off" and self.risk_off_exposure <= 0
                if regime_exit or bool(row["weak_exit"]) or bool(row["wedge_drop"]) or failed or stop_breached:
                    active = None
                    cooldown_until = index + self.cooldown_bars
                    exit_today = index == len(frame) - 1
                    continue
                crossback_add = (
                    stage >= 3
                    and active.signal == "wedge-pop"
                    and bool(row["crossback"])
                )
                active = _ActiveSignal(
                    **{
                        **active.__dict__,
                        "signal": "ema-crossback-add" if crossback_add else active.signal,
                        "exhausted": active.exhausted or (stage >= 5 and bool(row["exhaustion"])),
                    }
                )
                continue
            if index <= cooldown_until or row["regime"] == "risk-off" or not bool(row["leader"]):
                continue
            signal = None
            if stage == 1 and bool(row["long_trend"]):
                signal = "leader-trend"
            elif stage >= 2 and bool(row["wedge_pop"]):
                signal = "wedge-pop"
            elif stage >= 3 and bool(row["crossback"]):
                signal = "ema-crossback"
            elif stage >= 4 and bool(row["base_break"]):
                signal = "base-break"
            if signal is None or (
                row["regime"] == "neutral"
                and signal not in {"wedge-pop", "ema-crossback"}
            ):
                continue
            stop = self._entry_stop(signal, row)
            if not math.isfinite(stop) or stop >= close:
                continue
            active = _ActiveSignal(
                code=code,
                signal=signal,
                entry_close=close,
                stop=stop,
                pivot=float(row["prior_high10"]),
                entry_index=index,
                exhausted=False,
                score=float(row["return63_percentile"]),
            )
        return active, exit_today

    @staticmethod
    def _entry_stop(signal: str, row: pd.Series) -> float:
        ema_stop = float(row["ema20"] - 0.5 * row["atr"])
        if signal == "ema-crossback":
            return min(float(row["low"]), ema_stop)
        if signal == "base-break":
            return max(ema_stop, float(row["base_low10"] - 0.1 * row["atr"]))
        if signal == "leader-trend":
            return ema_stop
        return min(float(row["base_low10"]), ema_stop)

    def _weight(
        self,
        signal: _ActiveSignal,
        *,
        actual_entry: float | None = None,
        current_close: float | None = None,
    ) -> float:
        distance = max(signal.entry_close - signal.stop, signal.entry_close * 0.005)
        full_weight = min(
            self.max_position_weight,
            self.risk_per_trade * signal.entry_close / distance,
        )
        favorable = False
        if (
            signal.signal == "ema-crossback-add"
            and actual_entry is not None
            and current_close is not None
            and actual_entry > signal.stop
        ):
            actual_risk = actual_entry - signal.stop
            favorable = current_close >= actual_entry + 0.5 * actual_risk
        weight = full_weight if favorable else full_weight * self.initial_position_fraction
        return weight * (2.0 / 3.0 if signal.exhausted else 1.0)

    def generate_signals(self, dt: date) -> StrategyOutput:
        codes = [code for code in self.ctx.universe if self._is_main_board(code)]
        history = self.ctx.history(
            codes=codes,
            lookback=self.history_bars,
            fields=["open", "high", "low", "close", "volume", "amount"],
        )
        features: dict[str, pd.DataFrame] = {}
        for code in codes:
            try:
                frame = self._features(history.xs(code, level="code"))
            except (KeyError, TypeError, ValueError):
                continue
            if len(frame) >= self.history_bars:
                features[code] = frame
        if not features:
            return self._empty_output()

        dates = sorted(set().union(*(frame.index for frame in features.values())))
        breadth = pd.Series(index=dates, dtype=float)
        for day in dates:
            states = [
                bool(frame.at[day, "close"] > frame.at[day, "sma50"])
                for frame in features.values()
                if day in frame.index and pd.notna(frame.at[day, "sma50"])
            ]
            breadth.at[day] = sum(states) / len(states) if states else np.nan
        regimes = self._confirmed_regimes(breadth, self.risk_on_breadth, self.risk_off_breadth)

        for day in dates:
            returns20 = pd.Series({
                code: frame.at[day, "return20"]
                for code, frame in features.items()
                if day in frame.index
            }).dropna()
            returns63 = pd.Series({
                code: frame.at[day, "return63"]
                for code, frame in features.items()
                if day in frame.index
            }).dropna()
            percentile20 = returns20.rank(pct=True, method="average")
            percentile63 = returns63.rank(pct=True, method="average")
            for code, frame in features.items():
                if day not in frame.index:
                    continue
                frame.at[day, "return63_percentile"] = percentile63.get(code, np.nan)
                frame.at[day, "leader"] = bool(
                    percentile20.get(code, 0.0) >= self.leader_quantile
                    and percentile63.get(code, 0.0) >= self.leader_quantile
                    and frame.at[day, "avg_amount20"] >= self.min_avg_amount
                )
                frame.at[day, "regime"] = regimes.at[day]

        active: list[_ActiveSignal] = []
        exit_count = 0
        event_counts = {"wedge_pop": 0, "crossback": 0, "base_break": 0}
        for code, frame in features.items():
            marked = self._mark_events(frame)
            latest = marked.iloc[-1]
            for key in event_counts:
                event_counts[key] += int(bool(latest[key]))
            signal, exited = self._replay(code, marked)
            if signal is not None:
                active.append(signal)
            exit_count += int(exited)

        portfolio_positions = self.ctx.portfolio.positions if self.ctx.portfolio is not None else {}
        retained = [item for item in active if item.code in portfolio_positions]
        new_today = [
            item for item in active
            if item.code not in portfolio_positions
            and (
                item.entry_index == len(features[item.code]) - 1
                or (self.ablation_stage == "S1" and item.signal == "leader-trend")
            )
        ]
        selected = sorted(retained, key=lambda item: item.code)
        available_slots = max(0, self.max_positions - len(selected))
        selected.extend(
            sorted(new_today, key=lambda item: (-item.score, item.code))[:available_slots]
        )
        targets = {}
        for item in selected:
            position = portfolio_positions.get(item.code)
            targets[item.code] = self._weight(
                item,
                actual_entry=float(position.avg_cost) if position is not None else None,
                current_close=float(features[item.code].iloc[-1]["close"]),
            )
        current_regime = str(regimes.dropna().iloc[-1]) if not regimes.dropna().empty else "risk-off"
        exposure_limit = {
            "risk-on": self.risk_on_exposure,
            "neutral": self.neutral_exposure,
            "risk-off": self.risk_off_exposure,
        }[current_regime]
        gross = sum(targets.values())
        if gross > exposure_limit and gross > 0:
            scale = exposure_limit / gross
            targets = {code: weight * scale for code, weight in targets.items()}
        return StrategyOutput(targets, {
            "market_regime": current_regime,
            "breadth": float(breadth.dropna().iloc[-1]) if not breadth.dropna().empty else 0.0,
            "eligible_count": len(features),
            "selected_count": len(targets),
            "wedge_pop_count": event_counts["wedge_pop"],
            "crossback_count": event_counts["crossback"],
            "base_break_count": event_counts["base_break"],
            "exit_count": exit_count,
            "exhaustion_count": sum(item.exhausted for item in selected),
            "gross_target": float(sum(targets.values())),
            "selected_signals": [
                {"code": item.code, "signal": item.signal}
                for item in selected
            ],
        })

    @staticmethod
    def _empty_output() -> StrategyOutput:
        return StrategyOutput({}, {
            "market_regime": "risk-off",
            "breadth": 0.0,
            "eligible_count": 0,
            "selected_count": 0,
            "wedge_pop_count": 0,
            "crossback_count": 0,
            "base_break_count": 0,
            "exit_count": 0,
            "exhaustion_count": 0,
            "gross_target": 0.0,
            "selected_signals": [],
        })
