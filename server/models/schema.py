"""ORM models for server database"""
from datetime import datetime
from typing import Optional
import uuid
from sqlalchemy import String, Float, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column
from server.models.database import Base


def uuid4_str() -> str:
    return uuid.uuid4().hex[:12]


def now_str() -> str:
    return datetime.now().isoformat()


class Strategy(Base):
    __tablename__ = "strategy"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    strategy_class: Mapped[str] = mapped_column(String(255), nullable=False)
    params: Mapped[str] = mapped_column(Text, default="{}")  # JSON string
    created_at: Mapped[str] = mapped_column(String(30), default=now_str)
    updated_at: Mapped[str] = mapped_column(String(30), default=now_str)


class Run(Base):
    __tablename__ = "run"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    strategy_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    run_type: Mapped[str] = mapped_column(String(20), nullable=False)  # 'backtest' | 'paper'
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|running|completed|failed
    start_date: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    end_date: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    initial_capital: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    final_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    total_return: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    sharpe_ratio: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    max_drawdown: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    result_dir: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(30), default=now_str)
    completed_at: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)


class PaperSnapshot(Base):
    __tablename__ = "paper_snapshot"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[str] = mapped_column(String(30), nullable=False)
    cash: Mapped[float] = mapped_column(Float, default=0.0)
    market_value: Mapped[float] = mapped_column(Float, default=0.0)
    total_value: Mapped[float] = mapped_column(Float, default=0.0)
    daily_return: Mapped[float] = mapped_column(Float, default=0.0)
    n_positions: Mapped[int] = mapped_column(Integer, default=0)


class PaperPosition(Base):
    __tablename__ = "paper_position"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_date: Mapped[str] = mapped_column(String(30), nullable=False)
    code: Mapped[str] = mapped_column(String(20), nullable=False)
    shares: Mapped[int] = mapped_column(Integer, default=0)
    avg_cost: Mapped[float] = mapped_column(Float, default=0.0)
    market_value: Mapped[float] = mapped_column(Float, default=0.0)
    weight: Mapped[float] = mapped_column(Float, default=0.0)


class Deviation(Base):
    __tablename__ = "deviation"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[str] = mapped_column(String(30), nullable=False)
    paper_return: Mapped[float] = mapped_column(Float, default=0.0)
    expected_return: Mapped[float] = mapped_column(Float, default=0.0)
    tracking_error: Mapped[float] = mapped_column(Float, default=0.0)
