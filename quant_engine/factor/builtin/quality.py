"""质量因子"""

import numpy as np
import pandas as pd

from quant_engine.factor.base import Factor


class SharpeRatio1M(Factor):
    name = "sharpe_1m"
    category = "quality"
    inputs = ["close"]
    window = 20

    def compute(self, data: pd.DataFrame) -> pd.Series:
        returns = data["close"].pct_change()
        mu = returns.rolling(self.window).mean()
        sigma = returns.rolling(self.window).std()
        return mu / sigma.replace(0, np.nan)


class MaxDrawdown1M(Factor):
    name = "max_drawdown_1m"
    category = "quality"
    inputs = ["close"]
    window = 20

    def compute(self, data: pd.DataFrame) -> pd.Series:
        rolling_max = data["close"].rolling(self.window).max()
        drawdown = data["close"] / rolling_max - 1
        return drawdown
