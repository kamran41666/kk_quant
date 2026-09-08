"""回测引擎集成测试"""
import pytest
import json
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

import pandas as pd

from quant_engine.backtest.strategy import Strategy
from quant_engine.backtest.engine import BacktestEngine
from quant_engine.backtest.types import Trade, OrderSide


class SimpleTestStrategy(Strategy):
    """测试用简单策略: 等权持有前5只股票"""

    def initialize(self):
        self.top_n = 5
        self.fill_log = []

    def generate_signals(self, dt: date) -> dict[str, float]:
        stock_list = getattr(self.ctx, 'stock_list', [])
        n = min(self.top_n, len(stock_list))
        codes = stock_list[:n]
        weight = 1.0 / len(codes) if codes else 0.0
        return {c: weight for c in codes}

    def on_order_filled(self, trade: Trade):
        self.fill_log.append(trade)


class TestBacktestEngine:
    """回测引擎集成测试 — 使用 mock 数据避免网络请求"""

    @pytest.fixture
    def mock_daily_data(self):
        """构造模拟日线数据: MultiIndex (code, date) -> fields"""
        codes = ['000001.SZ', '000002.SZ', '000858.SZ',
                 '002415.SZ', '600000.SH']
        dates = pd.date_range('2024-01-02', '2024-03-29', freq='B')
        rows = []
        for code in codes:
            for d in dates:
                rows.append({
                    'code': code,
                    'date': d,
                    'open': 10.0,
                    'high': 10.5,
                    'low': 9.8,
                    'close': 10.0,
                    'volume': 1_000_000,
                    'amount': 10_000_000,
                    'turnover_rate': 0.5,
                    'up_limit': 11.0,
                    'down_limit': 9.0,
                    'is_suspended': False,
                })
        df = pd.DataFrame(rows)
        return df.set_index(['code', 'date']).sort_index()

    @pytest.fixture
    def mock_data_api(self, mock_daily_data):
        """Mock DataAPI 单例"""
        with patch('quant_engine.backtest.data_handler.DataAPI') as mock_cls:
            mock_instance = MagicMock()
            mock_instance.daily.return_value = mock_daily_data
            mock_cls.return_value = mock_instance
            yield mock_instance

    def test_engine_runs_without_error(self, mock_data_api):
        """引擎应该能够完整运行一个简短回测 (月度调仓)"""
        engine = BacktestEngine(
            SimpleTestStrategy,
            stock_list=[
                '000001.SZ', '000002.SZ', '000858.SZ',
                '002415.SZ', '600000.SH',
            ],
        )

        result_dir = engine.run(
            start=date(2024, 1, 2),
            end=date(2024, 3, 29),
            initial_capital=1_000_000.0,
            rebalance_frequency='monthly',
            output_dir='backtest_result/test_run',
        )

        # 验证输出文件存在
        result_path = Path(result_dir)
        assert result_path.exists()
        assert (result_path / "daily_portfolio.parquet").exists()
        assert (result_path / "summary.json").exists()

    def test_engine_fails_closed_when_calendar_is_unverified(self, monkeypatch):
        class UnverifiedCalendar:
            def __init__(self, start_year, end_year):
                pass

            def ensure_coverage(self, start, end):
                return {
                    "calendar_version": "trading-calendar-v1",
                    "source": "fallback:business-days",
                    "content_hash": "c" * 64,
                    "coverage_start": start.isoformat(),
                    "coverage_end": end.isoformat(),
                    "verified": False,
                    "complete": False,
                }

        monkeypatch.setattr("quant_engine.backtest.engine.TradingCalendar", UnverifiedCalendar)
        with pytest.raises(ValueError, match="trading_calendar_coverage_insufficient"):
            BacktestEngine(SimpleTestStrategy, stock_list=["000001.SZ"]).run(
                start=date(2024, 1, 2), end=date(2024, 1, 3),
                output_dir="backtest_result/test_unverified_calendar",
            )

    def test_engine_fails_closed_when_daily_coverage_is_incomplete(self, mock_data_api):
        mock_data_api.daily_coverage.return_value = {
            "coverage_version": "daily-coverage-v1",
            "market": "a-share",
            "source": "local:parquet",
            "start_date": "2024-01-02",
            "end_date": "2024-01-03",
            "requested_codes": ["000001.SZ"],
            "expected_trading_days": 2,
            "complete": False,
            "items": [{
                "code": "000001.SZ", "status": "partial",
                "missing_count": 1, "missing_dates": ["2024-01-03"],
            }],
            "coverage_hash": "d" * 64,
        }
        with pytest.raises(ValueError, match="daily_data_coverage_insufficient"):
            BacktestEngine(SimpleTestStrategy, stock_list=["000001.SZ"]).run(
                start=date(2024, 1, 2), end=date(2024, 1, 3),
                output_dir="backtest_result/test_incomplete_daily_coverage",
            )

    @staticmethod
    def _complete_daily_coverage(calendar_hash="c" * 64):
        return {
            "coverage_version": "daily-coverage-v1",
            "market": "a-share",
            "source": "local:parquet",
            "start_date": "2024-01-02",
            "end_date": "2024-01-03",
            "requested_codes": ["000001.SZ"],
            "expected_trading_days": 2,
            "complete": True,
            "calendar_content_hash": calendar_hash,
            "items": [{
                "code": "000001.SZ", "status": "complete", "content_hash": "d" * 64,
                "missing_count": 0, "missing_dates": [],
            }],
            "dataset_hash": "e" * 64,
            "coverage_hash": "f" * 64,
        }

    def test_engine_fails_closed_when_daily_read_fails_after_complete_coverage(self, mock_data_api):
        mock_data_api.daily_coverage.return_value = self._complete_daily_coverage()
        mock_data_api.daily.side_effect = RuntimeError("parquet read failed")
        with pytest.raises(ValueError, match="daily_data_load_failed"):
            BacktestEngine(SimpleTestStrategy, stock_list=["000001.SZ"]).run(
                start=date(2024, 1, 2), end=date(2024, 1, 3),
                output_dir="backtest_result/test_daily_read_failure",
            )

    def test_engine_blocks_when_coverage_uses_a_different_calendar(self, mock_data_api):
        mock_data_api.daily_coverage.return_value = self._complete_daily_coverage("a" * 64)
        with pytest.raises(ValueError, match="daily_data_calendar_mismatch"):
            BacktestEngine(SimpleTestStrategy, stock_list=["000001.SZ"]).run(
                start=date(2024, 1, 2), end=date(2024, 1, 3),
                output_dir="backtest_result/test_daily_calendar_mismatch",
            )

    def test_engine_with_weekly_rebalance(self, mock_data_api):
        """周频调仓也应该能运行"""
        engine = BacktestEngine(
            SimpleTestStrategy,
            stock_list=['000001.SZ', '000002.SZ', '000858.SZ'],
        )

        result_dir = engine.run(
            start=date(2024, 1, 2),
            end=date(2024, 2, 29),
            initial_capital=1_000_000.0,
            rebalance_frequency='weekly',
            output_dir='backtest_result/test_weekly',
        )

        result_path = Path(result_dir)
        assert result_path.exists()

    def test_summary_contains_expected_keys(self, mock_data_api):
        """摘要文件应包含预期的键"""
        engine = BacktestEngine(
            SimpleTestStrategy,
            stock_list=['000001.SZ', '000002.SZ'],
        )

        result_dir = engine.run(
            start=date(2024, 1, 2),
            end=date(2024, 1, 31),
            initial_capital=1_000_000.0,
            output_dir='backtest_result/test_summary',
        )

        with open(Path(result_dir) / "summary.json") as f:
            summary = json.load(f)

        assert "start_date" in summary
        assert "end_date" in summary
        assert summary["data_available_start"] == "2024-01-02"
        assert summary["data_available_end"] == "2024-01-31"
        assert summary["execution_model"] == "next_trading_day_open-v1"
        assert "final_value" in summary
        assert "total_return" in summary

    def test_summary_records_registered_cost_scenario(self, mock_data_api):
        engine = BacktestEngine(
            SimpleTestStrategy,
            stock_list=['000001.SZ'],
            cost_scenario="paper_high_impact_v1",
        )
        result_dir = engine.run(
            start=date(2024, 1, 2), end=date(2024, 1, 12),
            initial_capital=100_000.0,
            rebalance_frequency="daily",
            output_dir="backtest_result/test_cost_scenario",
        )
        with open(Path(result_dir) / "summary.json") as f:
            summary = json.load(f)
        assert summary["cost_scenario"] == "paper_high_impact_v1"
        assert summary["cost_model"]["research_only"] is True
        assert summary["cost_model"]["slippage_rate"] == pytest.approx(0.0025)

    def test_daily_rebalance(self, mock_data_api):
        """日频调仓也能运行"""
        engine = BacktestEngine(
            SimpleTestStrategy,
            stock_list=['000001.SZ', '000002.SZ'],
        )

        result_dir = engine.run(
            start=date(2024, 1, 2),
            end=date(2024, 1, 12),
            initial_capital=1_000_000.0,
            rebalance_frequency='daily',
            output_dir='backtest_result/test_daily',
        )

        result_path = Path(result_dir)
        assert result_path.exists()
        assert (result_path / "daily_portfolio.parquet").exists()

    def test_default_stock_pool_requires_point_in_time_snapshot(self, mock_data_api):
        """缺少历史成分时必须停止，不能静默使用生存者股票池"""
        # Make DataAPI index_components return empty list
        mock_data_api.index_components.return_value = []

        engine = BacktestEngine(SimpleTestStrategy)
        with pytest.raises(RuntimeError, match="Point-in-time stock pool unavailable"):
            engine.run(
                start=date(2024, 1, 2),
                end=date(2024, 1, 12),
                initial_capital=1_000_000.0,
                output_dir='backtest_result/test_fallback_pool',
            )

    def test_signals_to_orders_buy_new_position(self, mock_data_api):
        """信号转订单: 新买入一只股票"""
        from quant_engine.backtest.portfolio import Portfolio
        from quant_engine.backtest.order_manager import OrderManager
        from quant_engine.backtest.cost_model import CostModel

        engine = BacktestEngine(SimpleTestStrategy)
        cost = CostModel()
        om = OrderManager(cost)
        portfolio = Portfolio(initial_capital=1_000_000)

        # Mock data_handler that returns price=10.0
        dh = Mock()
        dh.get_price.return_value = 10.0

        signals = {'000001.SZ': 1.0}
        orders = engine._signals_to_orders(signals, portfolio, om, dh, date(2024, 1, 5))

        assert len(orders) == 1
        assert orders[0].code == '000001.SZ'
        assert orders[0].side == OrderSide.BUY
        assert orders[0].shares > 0

    def test_signals_to_orders_sell_existing(self, mock_data_api):
        """信号转订单: 卖出不在新信号中的持仓"""
        from quant_engine.backtest.portfolio import Portfolio
        from quant_engine.backtest.order_manager import OrderManager
        from quant_engine.backtest.cost_model import CostModel
        from quant_engine.backtest.types import Position
        from datetime import timedelta

        engine = BacktestEngine(SimpleTestStrategy)
        cost = CostModel()
        om = OrderManager(cost)
        portfolio = Portfolio(initial_capital=1_000_000)

        # Add a position (unlocked)
        dt = date(2024, 1, 5)
        portfolio._positions['000001.SZ'] = Position(
            code='000001.SZ', shares=1000, avg_cost=10.0,
            market_value=10000.0, unlock_date=dt - timedelta(days=1),
        )

        dh = Mock()
        dh.get_price.return_value = 10.0

        # Signal does NOT include 000001.SZ — it should be sold
        signals = {'000002.SZ': 1.0}
        orders = engine._signals_to_orders(signals, portfolio, om, dh, dt)

        # Should have one buy for 000002, one sell for 000001
        assert len(orders) == 2
        sell_orders = [o for o in orders if o.side == OrderSide.SELL]
        assert len(sell_orders) == 1
        assert sell_orders[0].code == '000001.SZ'

    def test_signals_to_orders_respects_t1_lock(self, mock_data_api):
        """信号转订单: T+1 锁仓的股票不应被卖出"""
        from quant_engine.backtest.portfolio import Portfolio
        from quant_engine.backtest.order_manager import OrderManager
        from quant_engine.backtest.cost_model import CostModel
        from quant_engine.backtest.types import Position

        engine = BacktestEngine(SimpleTestStrategy)
        cost = CostModel()
        om = OrderManager(cost)
        portfolio = Portfolio(initial_capital=1_000_000)

        # Position locked until tomorrow
        dt = date(2024, 1, 5)
        portfolio._positions['000001.SZ'] = Position(
            code='000001.SZ', shares=1000, avg_cost=10.0,
            market_value=10000.0, unlock_date=date(2024, 1, 6),  # locked today
        )

        dh = Mock()
        dh.get_price.return_value = 10.0

        signals = {'000002.SZ': 1.0}
        orders = engine._signals_to_orders(signals, portfolio, om, dh, dt)

        # 000001.SZ is T+1 locked, should NOT be sold
        sell_orders = [o for o in orders if o.side == OrderSide.SELL and o.code == '000001.SZ']
        assert len(sell_orders) == 0

    def test_strategy_kwargs_passed_correctly(self, mock_data_api):
        """Strategy kwargs 应该正确传递给策略"""
        received_kwargs = {}

        class KwargsCheckStrategy(Strategy):
            def initialize(self):
                received_kwargs.update(self._strategy_kwargs)
                self.top_n = self._strategy_kwargs.get('top_n', 5)

            def generate_signals(self, dt):
                stock_list = getattr(self.ctx, 'stock_list', [])
                n = min(self.top_n, len(stock_list))
                codes = stock_list[:n]
                w = 1.0 / len(codes) if codes else 0.0
                return {c: w for c in codes}

        engine = BacktestEngine(
            KwargsCheckStrategy,
            stock_list=['000001.SZ', '000002.SZ'],
            top_n=3,
        )

        engine.run(
            start=date(2024, 1, 2),
            end=date(2024, 1, 12),
            initial_capital=1_000_000.0,
            output_dir='backtest_result/test_kwargs',
        )

        assert received_kwargs.get('top_n') == 3

    def test_signal_executes_on_next_trading_day(self, mock_data_api, tmp_path):
        engine = BacktestEngine(
            SimpleTestStrategy,
            stock_list=['000001.SZ', '000002.SZ'],
        )
        result_dir = engine.run(
            start=date(2024, 1, 2),
            end=date(2024, 1, 12),
            initial_capital=1_000_000.0,
            rebalance_frequency='weekly',
            output_dir=str(tmp_path / 'lagged_execution'),
        )

        signals = pd.read_parquet(Path(result_dir) / 'signals.parquet')
        trades = pd.read_parquet(Path(result_dir) / 'trades.parquet')
        first_signal = pd.Timestamp(signals['date'].min())
        first_trade = pd.Timestamp(trades['date'].min())
        assert first_trade > first_signal
        assert first_signal.dayofweek == 4  # Friday
        assert first_trade.dayofweek == 0   # next Monday

    def test_strategy_teardown_runs_when_signal_generation_fails(self, mock_data_api, tmp_path):
        events = []

        class FailingStrategy(Strategy):
            def initialize(self):
                events.append("initialize")

            def generate_signals(self, dt):
                raise RuntimeError("signal failed")

            def teardown(self):
                events.append("teardown")

        engine = BacktestEngine(FailingStrategy, stock_list=["000001.SZ"])
        with pytest.raises(RuntimeError, match="signal failed"):
            engine.run(
                start=date(2024, 1, 2),
                end=date(2024, 1, 8),
                rebalance_frequency="daily",
                output_dir=str(tmp_path / "failed_strategy"),
            )
        assert events == ["initialize", "teardown"]

    def test_empty_target_liquidates_on_next_session(self, mock_data_api, tmp_path):
        class BuyThenCashStrategy(Strategy):
            def initialize(self):
                pass

            def generate_signals(self, dt):
                if dt == date(2024, 1, 5):
                    return {'000001.SZ': 0.5}
                return {}

        engine = BacktestEngine(
            BuyThenCashStrategy,
            stock_list=['000001.SZ'],
        )
        result_dir = engine.run(
            start=date(2024, 1, 2),
            end=date(2024, 1, 19),
            rebalance_frequency='weekly',
            output_dir=str(tmp_path / 'liquidate'),
        )
        trades = pd.read_parquet(Path(result_dir) / 'trades.parquet')
        assert trades['side'].tolist() == ['buy', 'sell']
        assert pd.Timestamp(trades.iloc[1]['date']) > pd.Timestamp(date(2024, 1, 12))
