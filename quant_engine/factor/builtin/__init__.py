"""内置因子库 — 导入即触发注册"""
from quant_engine.factor.builtin.momentum import (
    Momentum1M, Momentum3M, Momentum12M1M,
)
from quant_engine.factor.builtin.volatility import (
    Volatility1M, DownsideVolatility1M,
)
from quant_engine.factor.builtin.value import (
    LogMarketCap, LogMarketCapProxy, Turnover1M,
)
from quant_engine.factor.builtin.quality import (
    SharpeRatio1M, MaxDrawdown1M,
)
from quant_engine.factor.builtin.technical import (
    RSI14, MACD, BBandPosition,
)

__all__ = [
    "Momentum1M", "Momentum3M", "Momentum12M1M",
    "Volatility1M", "DownsideVolatility1M",
    "LogMarketCap", "LogMarketCapProxy", "Turnover1M",
    "SharpeRatio1M", "MaxDrawdown1M",
    "RSI14", "MACD", "BBandPosition",
]
