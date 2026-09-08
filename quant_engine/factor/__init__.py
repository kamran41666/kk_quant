"""因子研究模块 — 因子定义、计算、评估、合成。"""

# Importing the libraries is the explicit bootstrap point for both registries.
from quant_engine.factor import builtin as builtin
from quant_engine.factor import factors as factors
from quant_engine.factor.base import Factor, FactorMeta
from quant_engine.factor.catalog import (
    FactorDefinition,
    compute_factor,
    get_factor_definition,
    list_factor_definitions,
)

__all__ = [
    "Factor",
    "FactorDefinition",
    "FactorMeta",
    "compute_factor",
    "get_factor_definition",
    "list_factor_definitions",
]
