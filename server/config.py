"""Server configuration"""
from pathlib import Path
import os


class Settings:
    database_url: str = f"sqlite:///{Path(__file__).parent.parent / 'data' / 'server.db'}"
    data_dir: str = str(Path(__file__).parent.parent / "data")
    result_dir: str = str(Path(__file__).parent.parent / "backtest_result")
    scheduler_enabled: bool = os.getenv("QUANT_SCHEDULER_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
    scheduler_interval_seconds: int = max(10, int(os.getenv("QUANT_SCHEDULER_INTERVAL_SECONDS", "300")))


settings = Settings()
