"""Automatic strategy discovery and validated loading."""
from __future__ import annotations

import importlib
import inspect
import pkgutil
from dataclasses import dataclass
from quant_engine.backtest.protocol import StrategyProtocolError, StrategySpec


@dataclass(frozen=True)
class RegisteredStrategy:
    implementation: str
    strategy_class: type
    spec: StrategySpec

    def as_dict(self) -> dict:
        return self.spec.as_dict(self.implementation)


class StrategyRegistry:
    """Discovers concrete Strategy subclasses without database registration."""

    def __init__(self, package: str = "strategies"):
        self.package = package

    def discover(self) -> list[RegisteredStrategy]:
        from quant_engine.backtest.strategy import Strategy

        package = importlib.import_module(self.package)
        module_names = [package.__name__]
        if hasattr(package, "__path__"):
            module_names.extend(
                item.name for item in pkgutil.walk_packages(package.__path__, package.__name__ + ".")
            )
        entries: list[RegisteredStrategy] = []
        ids: dict[str, str] = {}
        for module_name in sorted(set(module_names)):
            module = importlib.import_module(module_name)
            for _, strategy_class in inspect.getmembers(module, inspect.isclass):
                if strategy_class is Strategy or not issubclass(strategy_class, Strategy):
                    continue
                if strategy_class.__name__.startswith("_"):
                    continue
                if strategy_class.__module__ != module.__name__ or inspect.isabstract(strategy_class):
                    continue
                spec = getattr(strategy_class, "SPEC", None)
                if not isinstance(spec, StrategySpec):
                    raise StrategyProtocolError(
                        f"{module.__name__}.{strategy_class.__name__} must declare SPEC: StrategySpec"
                    )
                implementation = f"{module.__name__}.{strategy_class.__name__}"
                if spec.id in ids:
                    raise StrategyProtocolError(
                        f"duplicate strategy id {spec.id}: {ids[spec.id]} and {implementation}"
                    )
                ids[spec.id] = implementation
                entries.append(RegisteredStrategy(implementation, strategy_class, spec))
        return sorted(entries, key=lambda item: item.spec.id)

    def load(self, implementation: str, *, require_spec: bool = True) -> RegisteredStrategy:
        from quant_engine.backtest.strategy import Strategy

        if not implementation.startswith(self.package + "."):
            raise StrategyProtocolError(f"strategy implementation must be inside {self.package} package")
        try:
            module_name, class_name = implementation.rsplit(".", 1)
            module = importlib.import_module(module_name)
            strategy_class = getattr(module, class_name)
        except (ImportError, AttributeError, ValueError) as exc:
            raise StrategyProtocolError(f"cannot load strategy implementation: {implementation}") from exc
        if not inspect.isclass(strategy_class) or not issubclass(strategy_class, Strategy):
            raise StrategyProtocolError(f"{implementation} is not a Strategy subclass")
        spec = getattr(strategy_class, "SPEC", None)
        if not isinstance(spec, StrategySpec):
            if require_spec:
                raise StrategyProtocolError(f"{implementation} does not declare a valid StrategySpec")
            spec = StrategySpec(
                id="legacy-" + class_name.lower(), name=class_name, version="legacy",
                description="Legacy strategy compatibility adapter", markets=("a-share", "cn-fund"),
            )
        return RegisteredStrategy(implementation, strategy_class, spec)


strategy_registry = StrategyRegistry()
