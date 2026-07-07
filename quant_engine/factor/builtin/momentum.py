"""动量因子"""

import pandas as pd

from quant_engine.factor.base import Factor


class Momentum1M(Factor):
    name = "momentum_1m"
    category = "momentum"
    inputs = ["close"]
    window = 20

    def compute(self, data: pd.DataFrame) -> pd.Series:
        return data["close"].pct_change(self.window)


class Momentum3M(Factor):
    name = "momentum_3m"
    category = "momentum"
    inputs = ["close"]
    window = 60

    def compute(self, data: pd.DataFrame) -> pd.Series:
        return data["close"].pct_change(self.window)


class Momentum12M1M(Factor):
    name = "momentum_12m_1m"
    category = "momentum"
    inputs = ["close"]
    window = 240

    def compute(self, data: pd.DataFrame) -> pd.Series:
        # 12个月收益减去最近1个月
        ret_12m = data["close"].pct_change(self.window)
        ret_1m = data["close"].pct_change(20)
        return ret_12m - ret_1m
