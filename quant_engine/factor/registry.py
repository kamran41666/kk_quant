"""因子注册表 — 单例，管理所有已注册的因子类"""
from typing import Optional, Type


class FactorRegistry:
    """全局因子注册表（单例）"""

    _instance: Optional["FactorRegistry"] = None

    def __new__(cls) -> "FactorRegistry":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._factors: dict[str, Type] = {}
        return cls._instance

    def register(self, factor_cls: Type):
        """注册因子类"""
        name = getattr(factor_cls, 'name', factor_cls.__name__)
        self._factors[name] = factor_cls

    def get(self, name: str) -> Optional[Type]:
        """按名称获取因子类"""
        return self._factors.get(name)

    def list_all(self) -> list[str]:
        """列出所有已注册因子名"""
        return list(self._factors.keys())

    def list_by_category(self, category: str) -> list[str]:
        """按分类列出因子"""
        return [
            name for name, cls in self._factors.items()
            if getattr(cls, 'category', None) == category
        ]

    def clear(self):
        """清空注册表（仅测试用）"""
        self._factors.clear()


# 全局单例
_registry = FactorRegistry()


def get_registry() -> FactorRegistry:
    return _registry
