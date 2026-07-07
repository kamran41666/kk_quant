"""Factor 基类 + FactorMeta 元类

使用元类自动注册: 定义 Factor 子类时自动注册到全局注册表。
"""
from abc import ABC, ABCMeta, abstractmethod
from typing import Optional

import pandas as pd

from quant_engine.factor.registry import get_registry


class FactorMeta(ABCMeta):
    """因子元类 — 子类定义时自动注册"""

    def __new__(mcs, name, bases, namespace):
        cls = super().__new__(mcs, name, bases, namespace)

        # 跳过 Factor 基类本身
        if name == 'Factor':
            return cls

        # 验证必要属性
        factor_name = namespace.get('name') or getattr(cls, 'name', None)
        if factor_name is None:
            raise TypeError(
                f"Factor subclass '{name}' must define a 'name' class attribute"
            )

        category = namespace.get('category') or getattr(cls, 'category', None)
        if category is None:
            raise TypeError(
                f"Factor '{factor_name}' must define a 'category' class attribute"
            )

        # 自动注册
        registry = get_registry()
        registry.register(cls)

        return cls


class Factor(ABC, metaclass=FactorMeta):
    """因子基类

    子类必须定义:
        name: str       — 因子名称 (如 "momentum_20")
        category: str   — 因子分类 (如 "momentum")
        compute(data)   — 计算逻辑

    可选定义:
        inputs: list[str]  — 需要的输入字段 (默认 ["close"])
        window: int        — 计算窗口 (默认 20)
        engine: str        — "python" 或 "cpp" (默认 "python")
    """

    name: str
    category: str
    inputs: list[str] = ["close"]
    window: int = 20
    engine: str = "python"

    @abstractmethod
    def compute(self, data: pd.DataFrame) -> pd.Series:
        """计算因子值

        Args:
            data: 单只股票的价格数据 DataFrame
                  columns: [open, high, low, close, volume, ...]
                  index: date

        Returns:
            因子值 Series，index 为 date
        """
        ...

    @classmethod
    def list_registered(cls) -> list[str]:
        """列出所有已注册的因子名称"""
        return get_registry().list_all()

    @classmethod
    def get(cls, name: str) -> Optional[type]:
        """按名称获取因子类"""
        return get_registry().get(name)

    @classmethod
    def list_by_category(cls, category: str) -> list[str]:
        """按分类列出因子"""
        return get_registry().list_by_category(category)

    def __repr__(self):
        return f"Factor({self.name}, category={self.category}, engine={self.engine})"
