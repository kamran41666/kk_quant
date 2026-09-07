"""事件驱动回测主循环

将 Strategy、DataHandler、RebalanceScheduler、OrderManager、Matcher、
Portfolio、Recorder 串联，按交易日迭代执行完整回测流程。
"""
import time
import json
from datetime import date
from pathlib import Path
from typing import Type

import pandas as pd

from quant_engine.data.calendar import TradingCalendar
from quant_engine.data.api import DataAPI
from quant_engine.backtest.types import OrderSide
from quant_engine.backtest.strategy import Strategy
from quant_engine.backtest.context import StrategyContext
from quant_engine.backtest.data_handler import DataHandler
from quant_engine.backtest.scheduler import RebalanceScheduler
from quant_engine.backtest.order_manager import OrderManager
from quant_engine.backtest.matcher import Matcher
from quant_engine.backtest.cost_model import CostModel
from quant_engine.backtest.portfolio import Portfolio
from quant_engine.backtest.recorder import Recorder


class BacktestCancelled(RuntimeError):
    """Raised when a bounded background run is cancelled or exceeds its deadline."""


class BacktestEngine:
    """事件驱动回测引擎

    用法:
        engine = BacktestEngine(MyAlphaStrategy, top_n=50)
        result_dir = engine.run(start=date(2020,1,1), end=date(2024,12,31))
    """

    def __init__(self, strategy_class: Type[Strategy], **strategy_kwargs):
        self._strategy_class = strategy_class
        self._stock_list = strategy_kwargs.pop("stock_list", None)
        # Cost scenarios are resolved from a small, versioned registry.  Keep
        # this outside strategy parameters so changing execution assumptions
        # creates different run evidence without changing the alpha code.
        self._cost_scenario = strategy_kwargs.pop("cost_scenario", "paper_baseline_v1")
        self._strategy_kwargs = strategy_kwargs

    def run(
        self,
        start: date,
        end: date,
        initial_capital: float = 1_000_000.0,
        benchmark: str = '000300.SH',
        output_dir: str = 'backtest_result',
        rebalance_frequency: str = 'weekly',
        rebalance_weekday: int = 5,
        cancel_check=None,
        max_seconds: float | None = None,
        calendar: TradingCalendar | None = None,
    ) -> str:
        """运行回测

        Args:
            start: 回测起始日期
            end: 回测结束日期
            initial_capital: 初始资金
            benchmark: 基准指数
            output_dir: 结果输出目录
            rebalance_frequency: 调仓频率 ('daily'/'weekly'/'monthly')
            rebalance_weekday: 周频调仓日 (1=Mon...5=Fri)

        Returns:
            结果目录路径
        """
        started_at = time.monotonic()
        # ---- 初始化 ----
        calendar = calendar or TradingCalendar(start_year=start.year, end_year=end.year)
        calendar_report = calendar.ensure_coverage(start, end)
        if not calendar_report.get("complete"):
            raise ValueError(
                "trading_calendar_coverage_insufficient: "
                + json.dumps(calendar_report, ensure_ascii=False, sort_keys=True)
            )
        trading_days = calendar.get_trading_days(start, end)

        if not trading_days:
            raise ValueError(f"No trading days between {start} and {end}")

        # 股票池: 从 strategy_kwargs 中获取，或默认用沪深300成分
        stock_list = self._get_stock_pool(trading_days[0])

        # 组件初始化
        context = StrategyContext(calendar)
        context.stock_list = stock_list

        strategy = self._strategy_class(context, **self._strategy_kwargs)
        strategy.initialize()

        data_handler = DataHandler(
            codes=stock_list,
            start=trading_days[0],
            end=trading_days[-1],
            calendar=calendar,
        )
        # A production DataAPI returns a structured coverage report.  Treat a
        # missing/invalid report as a compatibility condition for synthetic
        # test doubles, but never allow a real incomplete report or read error
        # to become a plausible partial backtest.
        data_coverage = data_handler.coverage_report
        if isinstance(data_coverage, dict):
            if data_handler.load_error:
                data_coverage = dict(data_coverage)
                data_coverage["load_error"] = data_handler.load_error
                raise ValueError(
                    "daily_data_load_failed: "
                    + json.dumps(data_coverage, ensure_ascii=False, sort_keys=True)
                )
            if not data_coverage.get("complete"):
                raise ValueError(
                    "daily_data_coverage_insufficient: "
                    + json.dumps(data_coverage, ensure_ascii=False, sort_keys=True)
                )
            expected_calendar_hash = calendar_report.get("content_hash")
            actual_calendar_hash = data_coverage.get("calendar_content_hash")
            if expected_calendar_hash and actual_calendar_hash != expected_calendar_hash:
                raise ValueError(
                    "daily_data_calendar_mismatch: "
                    f"engine={expected_calendar_hash} coverage={actual_calendar_hash}"
                )
            expected_codes = sorted(dict.fromkeys(stock_list))
            items = data_coverage.get("items")
            item_map = {
                str(item.get("code")): item
                for item in items
                if isinstance(item, dict) and item.get("code")
            } if isinstance(items, list) else {}
            if sorted(item_map) != expected_codes or any(
                item_map[code].get("status") != "complete"
                or not item_map[code].get("content_hash")
                for code in expected_codes
            ):
                raise ValueError(
                    "daily_data_coverage_invalid: "
                    + json.dumps(data_coverage, ensure_ascii=False, sort_keys=True)
                )
            if data_coverage.get("dataset_hash") != DataAPI.daily_dataset_hash(data_coverage):
                raise ValueError("daily_data_dataset_hash_invalid")
            if data_coverage.get("coverage_hash") != DataAPI.daily_coverage_hash(data_coverage):
                raise ValueError("daily_data_coverage_hash_invalid")
        elif data_handler.load_error:
            raise ValueError("daily_data_load_failed: " + data_handler.load_error)
        context._data_handler = data_handler
        scheduler = RebalanceScheduler(
            calendar,
            frequency=rebalance_frequency,
            weekday=rebalance_weekday,
        )
        cost_model = CostModel.from_scenario(self._cost_scenario)
        order_manager = OrderManager(cost_model)
        matcher = Matcher(cost_model)
        portfolio = Portfolio(initial_capital=initial_capital)
        recorder = Recorder(output_dir=output_dir)

        recorder.set_meta("strategy", self._strategy_class.__name__)
        recorder.set_meta("start_date", str(start))
        recorder.set_meta("end_date", str(end))
        recorder.set_meta("initial_capital", initial_capital)
        recorder.set_meta("benchmark", benchmark)
        recorder.set_meta("rebalance_frequency", rebalance_frequency)
        recorder.set_meta("execution_model", "next_trading_day_open-v1")
        recorder.set_meta("calendar_version", calendar_report["calendar_version"])
        recorder.set_meta("calendar_source", calendar_report["source"])
        recorder.set_meta("calendar_content_hash", calendar_report["content_hash"])
        recorder.set_meta("calendar_coverage_start", calendar_report["coverage_start"])
        recorder.set_meta("calendar_coverage_end", calendar_report["coverage_end"])
        recorder.set_meta("calendar_verified", calendar_report["verified"])
        recorder.set_meta("cost_model", cost_model.as_manifest())
        recorder.set_meta("cost_scenario", cost_model.scenario)
        # Keep requested bounds (start_date/end_date) separate from the
        # observed local-data bounds.  A successful loop over calendar days is
        # not evidence that every requested day had usable OHLCV rows.
        if data_handler.available_start is not None:
            recorder.set_meta("data_available_start", str(data_handler.available_start))
        if data_handler.available_end is not None:
            recorder.set_meta("data_available_end", str(data_handler.available_end))
        if isinstance(data_coverage, dict):
            recorder.set_meta("daily_data_coverage", data_coverage)
            if data_coverage.get("coverage_hash"):
                recorder.set_meta("daily_data_coverage_hash", data_coverage["coverage_hash"])

        pending_target: dict[str, float] | None = None
        pending_signal_date: date | None = None

        # ---- 主循环 ----
        for i, day in enumerate(trading_days):
            if cancel_check is not None and cancel_check():
                raise BacktestCancelled("backtest cancelled")
            if max_seconds is not None and time.monotonic() - started_at > max_seconds:
                raise BacktestCancelled("backtest deadline exceeded")
            data_handler.push_day(day)
            context.set_date(day)
            strategy.before_trading()

            # Execute the previous signal on the next trading day.  A strategy may
            # observe the signal day's close, so same-day OHLC/VWAP execution would
            # be a look-ahead violation.
            if pending_target is not None:
                orders = self._signals_to_orders(
                    pending_target, portfolio, order_manager, data_handler, day
                )
                # Release cash before funding buys during a rebalance.
                orders.sort(key=lambda item: 0 if item.side == OrderSide.SELL else 1)
                for order in orders:
                    trade = matcher.match(order, data_handler)
                    if trade is not None and not pd.isna(trade.price):
                        required_cash = (
                            trade.amount + trade.commission +
                            trade.stamp_duty + trade.slippage
                        )
                        if order.side == OrderSide.BUY and required_cash > portfolio.cash + 1e-9:
                            order.fill_shares = 0
                            order.reject("insufficient cash including fees")
                        else:
                            portfolio.apply_trade(trade)
                            recorder.record_trade(trade)
                            strategy.on_order_filled(trade)
                    # Record after matching so persisted status reflects the result.
                    recorder.record_order(order)
                pending_target = None

            # Rebalance day: generate a target for the *next* trading day.  An empty
            # mapping is a valid target and means liquidate all sellable holdings.
            if scheduler.is_rebalance_day(day):
                target = strategy.generate_signals(day)
                if not isinstance(target, dict):
                    raise TypeError("generate_signals() must return dict[str, float]")
                invalid = {
                    code: weight for code, weight in target.items()
                    if not isinstance(code, str) or not isinstance(weight, (int, float))
                    or pd.isna(weight) or weight < 0
                }
                if invalid or sum(target.values()) > 1.0 + 1e-9:
                    raise ValueError(f"Invalid target weights: {invalid or target}")

                total_val = portfolio.total_value
                old_weights = {}
                if total_val > 0:
                    for code, pos in portfolio.positions.items():
                        if pos.shares > 0:
                            old_weights[code] = pos.market_value / total_val
                strategy.on_rebalance(day, old_weights, target)
                recorder.record_signal(day, target)
                pending_target = dict(target)
                pending_signal_date = day

            # 每日估值（所有交易日）
            prices = {}
            for code in stock_list:
                p = data_handler.get_price(code, 'close')
                if not pd.isna(p):
                    prices[code] = float(p)

            portfolio.update_market_values(prices, day)
            context.set_portfolio(portfolio)

            # 日终记录
            recorder.record_positions(day, portfolio.positions)
            recorder.record_portfolio(day, portfolio)

        # ---- 结束 ----
        strategy.teardown()
        recorder.set_meta("final_value", portfolio.total_value)
        recorder.set_meta("total_return",
                          (portfolio.total_value - initial_capital) / initial_capital)
        recorder.set_meta("execution_lag", "next_trading_day")
        if pending_signal_date is not None and pending_target is not None:
            recorder.set_meta("unexecuted_signal_date", str(pending_signal_date))
        recorder.save()

        return str(Path(output_dir).resolve())

    def _get_stock_pool(self, first_day: date) -> list[str]:
        """获取初始股票池"""
        # 尝试从 strategy_kwargs 获取
        if self._stock_list is not None:
            codes = self._stock_list
            return codes if isinstance(codes, list) else list(codes)

        # 默认: 从 DataAPI 获取沪深300成分
        try:
            api = DataAPI()
            codes = api.index_components('000300', first_day)
            if codes:
                return codes[:50]  # 限制数量以加快回测
        except Exception:
            pass

        # Never silently replace a point-in-time index universe with a
        # hard-coded survivor list. That would make a backtest look successful
        # while introducing survivorship bias. Users can provide an explicit
        # stock_list, or import dated constituent snapshots first.
        raise RuntimeError(
            "Point-in-time stock pool unavailable for backtest date "
            f"{first_day}; provide stock_list or import historical index snapshots"
        )

    def _signals_to_orders(
        self,
        signals: dict[str, float],
        portfolio: Portfolio,
        order_manager: OrderManager,
        data_handler: DataHandler,
        dt: date,
    ) -> list:
        """将目标权重信号转换为订单列表

        逻辑:
        - 对每个目标股票，计算 target_shares = weight * total_value / price
        - 与当前持仓比较，差额部分生成买卖订单
        """
        orders = []
        total_value = portfolio.total_value

        for code, target_weight in signals.items():
            # Orders generated from the prior session's signal execute at the
            # next session's open.  Sizing against close/VWAP would either
            # disagree with the fill model or leak same-day information.
            price = data_handler.get_price(code, 'open')
            if target_weight <= 0:
                target_shares = 0
            else:
                if pd.isna(price) or price <= 0:
                    # Missing execution-day open means the order cannot be
                    # modelled without using a future close; skip it.
                    continue

                # 目标股数；为佣金和滑点预留现金，避免 100% 目标因
                # 交易费用导致整单被拒绝。
                target_amount = total_value * target_weight
                expected_price = float(price) * (1.0 + order_manager.cost_model.slippage_rate)
                expected_commission = max(
                    target_amount * order_manager.cost_model.commission_rate,
                    order_manager.cost_model.min_commission,
                )
                target_shares = order_manager.round_lot(
                    int(max(0.0, target_amount - expected_commission) / expected_price)
                )

            # 当前持仓
            current_pos = portfolio.positions.get(code)
            current_shares = current_pos.shares if current_pos else 0
            if target_shares <= 0 and current_shares <= 0:
                continue

            diff = target_shares - current_shares

            if diff > 0:
                # 买入差额
                order = order_manager.submit(
                    code=code,
                    side=OrderSide.BUY,
                    shares=diff,
                    dt=dt,
                )
                orders.append(order)

            elif diff < 0:
                # 卖出差额
                sellable = portfolio.get_sellable_shares(code, dt)
                sell_shares = min(abs(diff), sellable)
                if sell_shares > 0:
                    order = order_manager.submit(
                        code=code,
                        side=OrderSide.SELL,
                        shares=sell_shares,
                        dt=dt,
                    )
                    orders.append(order)

        # 清仓不在新信号中的股票
        new_codes = set(signals.keys())
        for code, pos in portfolio.positions.items():
            if code not in new_codes and pos.shares > 0:
                sellable = portfolio.get_sellable_shares(code, dt)
                if sellable > 0:
                    order = order_manager.submit(
                        code=code,
                        side=OrderSide.SELL,
                        shares=sellable,
                        dt=dt,
                    )
                    orders.append(order)

        return orders
