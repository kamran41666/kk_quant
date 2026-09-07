import pytest
from datetime import date
from quant_engine.backtest.types import Trade, OrderSide
from quant_engine.backtest.cost_model import CostModel, cost_scenario_catalog


class TestCostModel:
    @pytest.fixture
    def model(self):
        return CostModel(
            commission_rate=0.00025,
            stamp_duty_rate=0.001,
            min_commission=5.0,
            slippage_rate=0.001,
        )

    def test_buy_commission_only(self, model):
        """买入只交佣金, 不交印花税"""
        t = Trade(
            trade_id="t1", order_id="o1",
            code="000001.SZ", date=date(2024, 6, 14),
            side=OrderSide.BUY, shares=1000,
            price=10.0, amount=10000.0,
        )
        comm, stamp, slip = model.calc_cost(t)
        assert comm == pytest.approx(5.0)   # 10000*0.00025=2.5, floor=5
        assert stamp == 0.0                  # 买入不交
        assert slip == pytest.approx(10.0)   # 10000*0.001

    def test_sell_has_stamp_duty(self, model):
        """卖出交佣金+印花税"""
        t = Trade(
            trade_id="t2", order_id="o2",
            code="000001.SZ", date=date(2024, 6, 14),
            side=OrderSide.SELL, shares=1000,
            price=10.0, amount=10000.0,
        )
        comm, stamp, slip = model.calc_cost(t)
        assert stamp == pytest.approx(10.0)   # 10000*0.001

    def test_large_trade_no_min_commission(self, model):
        """大额交易佣金超过最低5元"""
        t = Trade(
            trade_id="t3", order_id="o3",
            code="000001.SZ", date=date(2024, 6, 14),
            side=OrderSide.BUY, shares=100000,
            price=10.0, amount=1_000_000.0,
        )
        comm, _, _ = model.calc_cost(t)
        assert comm == pytest.approx(250.0)    # 1M*0.00025

    def test_est_fill_price_buy(self, model):
        """买入成交价 = 参考价 * (1 + slippage)"""
        fill = model.est_fill_price(ref_price=10.0, side=OrderSide.BUY)
        assert fill > 10.0

    def test_est_fill_price_sell(self, model):
        """卖出成交价 = 参考价 * (1 - slippage)"""
        fill = model.est_fill_price(ref_price=10.0, side=OrderSide.SELL)
        assert fill < 10.0

    def test_custom_rates(self):
        """可自定义费率"""
        model = CostModel(
            commission_rate=0.0001, stamp_duty_rate=0.0005, slippage_rate=0.0
        )
        t = Trade(
            trade_id="t4", order_id="o4",
            code="000001.SZ", date=date(2024, 6, 14),
            side=OrderSide.BUY, shares=10000,
            price=10.0, amount=100000.0,
        )
        comm, _, slip = model.calc_cost(t)
        assert comm == pytest.approx(10.0)     # 100000*0.0001
        assert slip == 0.0

    def test_total_cost(self, model):
        t = Trade(
            trade_id="t5", order_id="o5",
            code="000001.SZ", date=date(2024, 6, 14),
            side=OrderSide.BUY, shares=1000,
            price=10.0, amount=10000.0,
            commission=5.0, stamp_duty=0.0, slippage=10.0,
        )
        assert model.total_cost(t) == pytest.approx(15.0)

    def test_registered_scenarios_are_explicit_and_reproducible(self):
        catalog = cost_scenario_catalog()
        assert [item["id"] for item in catalog] == [
            "paper_baseline_v1", "paper_low_impact_v1", "paper_high_impact_v1",
        ]
        baseline = CostModel.from_scenario("paper_baseline_v1")
        high = CostModel.from_scenario("paper_high_impact_v1")
        assert baseline.as_manifest()["research_only"] is True
        assert high.slippage_rate > baseline.slippage_rate
        assert high.as_manifest()["scenario"] == "paper_high_impact_v1"

    def test_unknown_scenario_fails_closed(self):
        with pytest.raises(ValueError, match="unknown cost scenario"):
            CostModel.from_scenario("broker_default")
