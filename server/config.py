"""Server configuration"""
from pathlib import Path


class Settings:
    database_url: str = f"sqlite:///{Path(__file__).parent.parent / 'data' / 'server.db'}"
    data_dir: str = str(Path(__file__).parent.parent / "data")
    result_dir: str = str(Path(__file__).parent.parent / "backtest_result")
    scheduler_enabled: bool = True


settings = Settings()
