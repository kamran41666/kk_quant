"""技术因子"""

import numpy as np
import pandas as pd

from quant_engine.factor.base import Factor


class RSI14(Factor):
    name = "rsi_14"
    category = "technical"
    inputs = ["close"]
    window = 14

    def compute(self, data: pd.DataFrame) -> pd.Series:
        delta = data["close"].diff()
        gain = delta.where(delta > 0, 0.0)
        loss = (-delta).where(delta < 0, 0.0)
        avg_gain = gain.rolling(self.window).mean()
        avg_loss = loss.rolling(self.window).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        return 100.0 - (100.0 / (1.0 + rs))


class MACD(Factor):
    name = "macd"
    category = "technical"
    inputs = ["close"]
    window = 26

    def compute(self, data: pd.DataFrame) -> pd.Series:
        ema_12 = data["close"].ewm(span=12, adjust=False).mean()
        ema_26 = data["close"].ewm(span=26, adjust=False).mean()
        return ema_12 - ema_26


class BBandPosition(Factor):
    name = "bb_position"
    category = "technical"
    inputs = ["close"]
    window = 20

    def compute(self, data: pd.DataFrame) -> pd.Series:
        sma = data["close"].rolling(self.window).mean()
        std = data["close"].rolling(self.window).std()
        return (data["close"] - sma) / (2 * std)
