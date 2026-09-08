"""Factor catalogue for vectorized multi-asset expressions."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import pandas as pd

FactorFunction = Callable[[pd.DataFrame], pd.Series]


@dataclass(frozen=True)
class FactorDefinition:
    """Metadata needed by research and point-in-time backtest adapters."""

    name: str
    category: str
    inputs: tuple[str, ...]
    window: int
    formula: str
    logic: str
    operators: tuple[str, ...]
    compute: FactorFunction


_CATALOG: dict[str, FactorDefinition] = {}


def register_factor(
    *,
    name: str,
    category: str,
    inputs: tuple[str, ...],
    window: int,
    formula: str,
    logic: str,
    operators: tuple[str, ...],
) -> Callable[[FactorFunction], FactorFunction]:
    """Register a named factor and reject accidental silent replacement."""
    if not name or window < 1:
        raise ValueError("factor name and window >= 1 are required")

    def decorate(function: FactorFunction) -> FactorFunction:
        if name in _CATALOG:
            raise ValueError(f"duplicate factor name: {name}")
        _CATALOG[name] = FactorDefinition(
            name=name,
            category=category,
            inputs=inputs,
            window=window,
            formula=formula,
            logic=logic,
            operators=operators,
            compute=function,
        )
        return function

    return decorate


def get_factor_definition(name: str) -> FactorDefinition:
    try:
        return _CATALOG[name]
    except KeyError as exc:
        available = ", ".join(sorted(_CATALOG))
        raise KeyError(f"unknown panel factor {name!r}; available: {available}") from exc


def list_factor_definitions(category: str | None = None) -> tuple[FactorDefinition, ...]:
    definitions = _CATALOG.values()
    if category is not None:
        definitions = (item for item in definitions if item.category == category)
    return tuple(sorted(definitions, key=lambda item: item.name))


def compute_factor(name: str, data: pd.DataFrame) -> pd.Series:
    definition = get_factor_definition(name)
    missing = sorted(set(definition.inputs) - set(data.columns))
    if missing:
        raise ValueError(f"factor {name} requires missing fields: {', '.join(missing)}")
    result = definition.compute(data)
    if not isinstance(result, pd.Series) or not result.index.equals(data.index):
        raise ValueError(f"factor {name} must return a Series aligned to its input")
    return result.rename(name)
