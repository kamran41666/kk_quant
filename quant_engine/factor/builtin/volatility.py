"""波动率因子"""

import numpy as np
import pandas as pd

from quant_engine.factor.base import Factor


class Volatility1M(Factor):
    name = "volatility_1m"
    category = "volatility"
    inputs = ["close"]
    window = 20

    def compute(self, data: pd.DataFrame) -> pd.Series:
        returns = data["close"].pct_change()
        return returns.rolling(self.window).std()


class DownsideVolatility1M(Factor):
    name = "downside_vol_1m"
    category = "volatility"
    inputs = ["close"]
    window = 20

    def compute(self, data: pd.DataFrame) -> pd.Series:
        returns = data["close"].pct_change()
        downside = returns.where(returns < 0, 0)
        return downside.rolling(self.window).std()
