"""Strategy 基类 — 用户策略通过继承此类并重写钩子方法来实现"""
from abc import ABC, abstractmethod
from datetime import date

from quant_engine.backtest.context import StrategyContext
from quant_engine.backtest.types import Trade


class Strategy(ABC):
    """策略基类

    用户继承此类，重写以下钩子方法实现自定义策略：

    必须重写:
        initialize() — 设置参数、注册因子
        generate_signals(date) — 核心信号逻辑

    可选重写:
        before_trading() — 盘前准备
        on_rebalance(date, old, new) — 调仓回调
        on_order_filled(trade) — 成交通知
        teardown() — 结束处理
    """

    def __init__(self, context: StrategyContext, **kwargs):
        self.ctx = context
        self._initialized = False
        self._strategy_kwargs = kwargs

    # ===== 必须重写 =====

    @abstractmethod
    def initialize(self):
        """策略初始化 — 设置参数、注册需要的因子

        示例:
            self.top_n = 50
            self.use_factor("momentum_20")
        """
        ...

    @abstractmethod
    def generate_signals(self, dt: date) -> dict[str, float]:
        """生成调仓信号

        仅在调仓日被引擎调用（默认每周五）。

        Args:
            dt: 当前调仓日期

        Returns:
            {code: target_weight} — 目标权重字典，权重之和为 1.0
            如 {'000001.SZ': 0.02, '000002.SZ': 0.02, ...}
        """
        ...

    # ===== 可选重写 =====

    def before_trading(self):
        """盘前准备 — 每个交易日调用一次"""
        pass

    def on_rebalance(
        self, dt: date,
        old_weights: dict[str, float],
        new_weights: dict[str, float]
    ):
        """调仓日回调 — 旧持仓 → 新持仓切换时调用

        Args:
            dt: 调仓日期
            old_weights: 调仓前的持仓权重
            new_weights: 调仓后的目标权重
        """
        pass

    def on_order_filled(self, trade: Trade):
        """成交回调 — 每笔订单成交时调用"""
        pass

    def teardown(self):
        """结束回调 — 回测完成时调用"""
        pass

    # ===== 辅助方法 =====

    def log(self, message: str):
        """记录日志"""
        self.ctx.log(message)

    def use_factor(self, name: str):
        """注册因子 — 策略声明需要使用的因子（当前阶段仅记录）"""
        if not hasattr(self, '_registered_factors'):
            self._registered_factors = []
        if name not in self._registered_factors:
            self._registered_factors.append(name)

    def get_factor(self, name: str, dt: date):
        """获取因子数据"""
        return self.ctx.get_factor(name, dt)
