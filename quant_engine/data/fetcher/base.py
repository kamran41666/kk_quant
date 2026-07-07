"""数据源抽象接口"""
from abc import ABC, abstractmethod
from datetime import date
import pandas as pd


class DataSource(ABC):
    @abstractmethod
    def fetch_daily(
        self, codes: list[str], start: date, end: date
    ) -> pd.DataFrame:
        """拉取日线行情"""
        ...

    @abstractmethod
    def fetch_stock_list(self) -> pd.DataFrame:
        """拉取全市场股票列表"""
        ...

    @abstractmethod
    def fetch_index_components(
        self, index_code: str, dt: date
    ) -> pd.DataFrame:
        """拉取指数成分股在某日期的构成"""
        ...

    @property
    @abstractmethod
    def source_name(self) -> str:
        """数据源名称"""
        ...
