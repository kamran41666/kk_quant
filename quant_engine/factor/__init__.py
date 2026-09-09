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
from quant_engine.factor.expression import (
    EXPRESSION_PROTOCOL_VERSION,
    FactorExpressionError,
    FactorExpressionSpec,
)
from quant_engine.factor.gates import FactorGateError, FactorGatePolicy
from quant_engine.factor.generation import FactorCandidateGenerator, GENERATORS
from quant_engine.factor.frozen import FrozenFactorStrategyBundle
from quant_engine.factor.strategy import build_expression_rank_strategy

__all__ = [
    "Factor",
    "FactorDefinition",
    "FactorMeta",
    "FactorExpressionError",
    "FactorExpressionSpec",
    "EXPRESSION_PROTOCOL_VERSION",
    "FactorGateError",
    "FactorGatePolicy",
    "FactorCandidateGenerator",
    "GENERATORS",
    "FrozenFactorStrategyBundle",
    "build_expression_rank_strategy",
    "compute_factor",
    "get_factor_definition",
    "list_factor_definitions",
]
