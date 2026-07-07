"""估值/规模/流动性因子"""

import numpy as np
import pandas as pd

from quant_engine.factor.base import Factor


class LogMarketCap(Factor):
    name = "log_market_cap"
    category = "size"
    inputs = ["close", "volume"]
    window = 1

    def compute(self, data: pd.DataFrame) -> pd.Series:
        # 简化: 用 close * volume 近似市值
        approx_mcap = data["close"] * data["volume"]
        return np.log(approx_mcap.replace(0, np.nan))


class Turnover1M(Factor):
    name = "turnover_1m"
    category = "liquidity"
    inputs = ["turnover_rate"]
    window = 20

    def compute(self, data: pd.DataFrame) -> pd.Series:
        if "turnover_rate" in data.columns:
            return data["turnover_rate"].rolling(self.window).mean()
        # 回退: 用 volume 近似
        return data["volume"].rolling(self.window).mean()
