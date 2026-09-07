"""估值/规模/流动性因子"""

import numpy as np
import pandas as pd

from quant_engine.factor.base import Factor


class LogMarketCap(Factor):
    """Log of point-in-time market capitalization.

    ``market_cap`` must be supplied by a fundamentals/share-count provider in
    the same currency and as-of date as the price. It is deliberately not
    inferred from trading volume.
    """
    name = "log_market_cap"
    category = "size"
    inputs = ["market_cap"]
    window = 1
    is_proxy = False
    definition = "ln(point-in-time market capitalization; market_cap is currency units)"

    def compute(self, data: pd.DataFrame) -> pd.Series:
        if "market_cap" not in data.columns:
            raise ValueError("log_market_cap requires point-in-time market_cap data")
        market_cap = pd.to_numeric(data["market_cap"], errors="coerce")
        return np.log(market_cap.where(market_cap > 0))


class LogMarketCapProxy(Factor):
    """Turnover-derived size proxy used only by the demo strategy."""
    name = "log_market_cap_proxy"
    category = "size"
    inputs = ["amount", "turnover_rate"]
    window = 1
    is_proxy = True
    definition = "ln(amount / (turnover_rate / 100)); proxy, not market capitalization"

    def compute(self, data: pd.DataFrame) -> pd.Series:
        required = {"amount", "turnover_rate"}
        if not required.issubset(data.columns):
            raise ValueError("log_market_cap_proxy requires amount and turnover_rate")
        amount = pd.to_numeric(data["amount"], errors="coerce")
        turnover = pd.to_numeric(data["turnover_rate"], errors="coerce").replace(0, np.nan)
        proxy = amount / (turnover / 100.0)
        return np.log(proxy.where(proxy > 0))


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
