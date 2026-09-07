"""ORM models for server database"""
from datetime import datetime
from typing import Optional
import uuid
from sqlalchemy import Boolean, String, Float, Integer, Text, UniqueConstraint
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
    # A strategy's market is part of its identity.  Legacy rows default to
    # A-share; cross-market observation must match this value explicitly.
    market: Mapped[str] = mapped_column(String(20), nullable=False, default="a-share")
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
    # Immutable evidence captured at run creation/completion.  Legacy rows
    # remain nullable and are treated as legacy A-share runs until re-run.
    market: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    strategy_fingerprint: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    data_manifest: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    data_end: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    calendar_version: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    execution_model: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    eligible_for_observation: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
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


class PaperAccount(Base):
    """Durable paper account header; no broker credentials are stored here."""
    __tablename__ = "paper_account"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    initial_capital: Mapped[float] = mapped_column(Float, nullable=False)
    cash: Mapped[float] = mapped_column(Float, nullable=False)
    # A paper account is single-market so CNY and USD balances can never be
    # accidentally added together. Existing accounts are A-share by default.
    market: Mapped[str] = mapped_column(String(20), nullable=False, default="a-share")
    status: Mapped[str] = mapped_column(String(20), default="active")
    max_order_notional: Mapped[float] = mapped_column(Float, default=100_000.0)
    max_position_weight: Mapped[float] = mapped_column(Float, default=0.25)
    max_daily_loss: Mapped[float] = mapped_column(Float, default=0.03)
    created_at: Mapped[str] = mapped_column(String(30), default=now_str)
    updated_at: Mapped[str] = mapped_column(String(30), default=now_str)


class PaperAccountPosition(Base):
    __tablename__ = "paper_account_position"
    __table_args__ = (UniqueConstraint("account_id", "code", name="uq_paper_account_code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    market: Mapped[str] = mapped_column(String(20), nullable=False, default="a-share")
    code: Mapped[str] = mapped_column(String(20), nullable=False)
    shares: Mapped[float] = mapped_column(Float, default=0.0)
    avg_cost: Mapped[float] = mapped_column(Float, default=0.0)
    market_value: Mapped[float] = mapped_column(Float, default=0.0)
    last_price: Mapped[float] = mapped_column(Float, default=0.0)
    last_price_source: Mapped[str] = mapped_column(String(80), default="manual_input")
    last_price_as_of: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    last_price_freshness: Mapped[str] = mapped_column(String(20), default="manual")
    updated_at: Mapped[str] = mapped_column(String(30), default=now_str)


class PaperLot(Base):
    """Per-buy lot used to enforce A-share T+1 sell availability."""
    __tablename__ = "paper_lot"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    market: Mapped[str] = mapped_column(String(20), nullable=False, default="a-share")
    code: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    remaining_quantity: Mapped[float] = mapped_column(Float, nullable=False)
    buy_price: Mapped[float] = mapped_column(Float, nullable=False)
    buy_at: Mapped[str] = mapped_column(String(30), nullable=False)
    unlock_date: Mapped[str] = mapped_column(String(10), nullable=False)
    owner: Mapped[str] = mapped_column(String(20), nullable=False, default="manual")
    # Strategy lots are scoped to one observation task.  ``None`` preserves
    # compatibility for legacy/manual lots created before Phase 4C.
    owner_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    created_at: Mapped[str] = mapped_column(String(30), default=now_str)


class PaperOrder(Base):
    __tablename__ = "paper_order"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    account_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    market: Mapped[str] = mapped_column(String(20), nullable=False, default="a-share")
    code: Mapped[str] = mapped_column(String(20), nullable=False)
    side: Mapped[str] = mapped_column(String(10), nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    limit_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    filled_quantity: Mapped[float] = mapped_column(Float, default=0.0)
    fill_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    reject_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    price_source: Mapped[str] = mapped_column(String(80), default="manual_input")
    price_as_of: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    price_freshness: Mapped[str] = mapped_column(String(20), default="manual")
    created_at: Mapped[str] = mapped_column(String(30), default=now_str)


class PaperFill(Base):
    __tablename__ = "paper_fill"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    order_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    account_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    market: Mapped[str] = mapped_column(String(20), nullable=False, default="a-share")
    code: Mapped[str] = mapped_column(String(20), nullable=False)
    side: Mapped[str] = mapped_column(String(10), nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[str] = mapped_column(String(30), default=now_str)


class PaperLedgerEvent(Base):
    __tablename__ = "paper_ledger_event"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    account_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(30), nullable=False)
    reference_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    payload: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[str] = mapped_column(String(30), default=now_str)


class PaperValuation(Base):
    """Durable mark-to-market checkpoints used by risk and the UI."""
    __tablename__ = "paper_valuation"
    __table_args__ = (UniqueConstraint("account_id", "valuation_date", name="uq_paper_valuation_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    valuation_date: Mapped[str] = mapped_column(String(10), nullable=False)
    cash: Mapped[float] = mapped_column(Float, nullable=False)
    market_value: Mapped[float] = mapped_column(Float, nullable=False)
    equity: Mapped[float] = mapped_column(Float, nullable=False)
    daily_return: Mapped[float] = mapped_column(Float, default=0.0)
    price_source: Mapped[str] = mapped_column(String(80), default="manual_input")
    price_as_of: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    price_freshness: Mapped[str] = mapped_column(String(20), default="manual")
    # Per-position provenance for multi-asset valuations.  The aggregate
    # fields above remain for backwards compatibility and quick summaries.
    price_metadata: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[str] = mapped_column(String(30), default=now_str)


class FundNavEvidence(Base):
    """Provider evidence that authorizes a paper fund NAV order or valuation.

    The market endpoint writes the exact provider observation here.  Paper
    execution later matches code, value, source, timestamp and freshness
    against this durable evidence instead of trusting caller-supplied
    labels.  Keeping it durable allows a restart or a second local worker to
    reuse a quote that was actually ingested by this application.
    """
    __tablename__ = "fund_nav_evidence"
    __table_args__ = (UniqueConstraint("code", "source", "as_of", "freshness", name="uq_fund_nav_evidence_observation"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    source: Mapped[str] = mapped_column(String(80), nullable=False)
    as_of: Mapped[str] = mapped_column(String(40), nullable=False)
    freshness: Mapped[str] = mapped_column(String(20), nullable=False)
    received_at: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    created_at: Mapped[str] = mapped_column(String(30), default=now_str)


class FundNavDataset(Base):
    """Immutable, content-addressed daily NAV dataset used by fund research.

    A provider response is an observation; a backtest needs a stable input.
    The dataset id is the SHA-256 content hash, so a corrected upstream NAV
    creates a new dataset instead of changing an old run's inputs.
    """
    __tablename__ = "fund_nav_dataset"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    code: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(80), nullable=False)
    start_date: Mapped[str] = mapped_column(String(10), nullable=False)
    end_date: Mapped[str] = mapped_column(String(10), nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), default=now_str)


class FundNavDatasetRow(Base):
    """Canonical rows belonging to one immutable fund NAV dataset."""
    __tablename__ = "fund_nav_dataset_row"
    __table_args__ = (UniqueConstraint("dataset_id", "nav_date", name="uq_fund_nav_dataset_row_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dataset_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    nav_date: Mapped[str] = mapped_column(String(10), nullable=False)
    nav: Mapped[float] = mapped_column(Float, nullable=False)
    change_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=now_str)


class PaperDeviation(Base):
    """Expected-vs-realized checkpoint for one account and trading date."""
    __tablename__ = "paper_deviation"
    __table_args__ = (UniqueConstraint("account_id", "valuation_date", name="uq_paper_deviation_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    valuation_date: Mapped[str] = mapped_column(String(10), nullable=False)
    expected_return: Mapped[float] = mapped_column(Float, nullable=False)
    actual_return: Mapped[float] = mapped_column(Float, nullable=False)
    tracking_error: Mapped[float] = mapped_column(Float, nullable=False)
    source: Mapped[str] = mapped_column(String(80), default="manual_plan")
    created_at: Mapped[str] = mapped_column(String(30), default=now_str)


class PaperDailyReport(Base):
    """Durable daily report generated after valuation or reconciliation."""
    __tablename__ = "paper_daily_report"
    __table_args__ = (UniqueConstraint("account_id", "report_date", name="uq_paper_daily_report_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    report_date: Mapped[str] = mapped_column(String(10), nullable=False)
    equity: Mapped[float] = mapped_column(Float, nullable=False)
    daily_return: Mapped[float] = mapped_column(Float, nullable=False)
    total_return: Mapped[float] = mapped_column(Float, nullable=False)
    max_drawdown: Mapped[float] = mapped_column(Float, nullable=False)
    order_count: Mapped[int] = mapped_column(Integer, default=0)
    fill_count: Mapped[int] = mapped_column(Integer, default=0)
    reconciliation_status: Mapped[str] = mapped_column(String(20), default="not_run")
    created_at: Mapped[str] = mapped_column(String(30), default=now_str)


class PaperSchedulerRun(Base):
    """Idempotent record of the automatic trading-day task."""
    __tablename__ = "paper_scheduler_run"
    __table_args__ = (UniqueConstraint("run_date", name="uq_paper_scheduler_run_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_date: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    account_count: Mapped[int] = mapped_column(Integer, default=0)
    valuation_count: Mapped[int] = mapped_column(Integer, default=0)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(30), default=now_str)
    # Watermark used to decide whether a later order requires a same-day
    # revaluation.  Keep creation time immutable for auditability.
    last_run_at: Mapped[str] = mapped_column(String(40), default=now_str)


class StrategyObservation(Base):
    """A bounded, paper-only observation of a backtested strategy."""
    __tablename__ = "strategy_observation"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_observation_idempotency"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    account_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    strategy_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    # Exact completed backtest evidence used to authorize this observation.
    # Nullable for legacy rows created before evidence binding was introduced.
    backtest_run_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    market: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    strategy_fingerprint: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    duration_days: Mapped[int] = mapped_column(Integer, nullable=False)
    allocation_pct: Mapped[float] = mapped_column(Float, nullable=False)
    allocated_capital: Mapped[float] = mapped_column(Float, nullable=False)
    start_date: Mapped[str] = mapped_column(String(10), nullable=False)
    end_date: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    auto_trade: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    started_at: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    paused_at: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    stopped_at: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    last_tick_date: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    last_tick_at: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    last_tick_status: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Codes currently owned by the strategy observation.  Manual holdings
    # outside this set remain user-controlled and are never auto-sold by a
    # subsequent strategy rebalance.
    managed_codes: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    # Live paper observations stage a signal on D and execute it only on a
    # later valid checkpoint.  Keeping the target durable makes retries and
    # process restarts deterministic.
    pending_signals: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    pending_signal_date: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=now_str)
    updated_at: Mapped[str] = mapped_column(String(40), default=now_str)


class StrategyObservationEvent(Base):
    """Append-only event trail for observation state and tick decisions."""
    __tablename__ = "strategy_observation_event"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    observation_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    event_date: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    signal_count: Mapped[int] = mapped_column(Integer, default=0)
    order_count: Mapped[int] = mapped_column(Integer, default=0)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    payload: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[str] = mapped_column(String(40), default=now_str)


class PaperRebalancePlan(Base):
    """Durable execution envelope for one strategy rebalance.

    The plan is written before any order is submitted.  Each item has its own
    idempotency key, so a worker crash after a fill can safely resume without
    creating a duplicate paper order.
    """
    __tablename__ = "paper_rebalance_plan"
    __table_args__ = (UniqueConstraint("observation_id", "signal_date", "execution_date", name="uq_paper_rebalance_window"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    observation_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    account_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    market: Mapped[str] = mapped_column(String(20), nullable=False, default="cn-fund")
    signal_date: Mapped[str] = mapped_column(String(10), nullable=False)
    execution_date: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="planned")
    order_count: Mapped[int] = mapped_column(Integer, default=0)
    filled_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # A short-lived lease prevents two scheduler workers from submitting the
    # same plan concurrently. A crashed worker can be reclaimed after expiry.
    lease_owner: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    lease_until: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=now_str)
    updated_at: Mapped[str] = mapped_column(String(40), default=now_str)


class PaperRebalanceItem(Base):
    """One deterministic paper order in a durable rebalance plan."""
    __tablename__ = "paper_rebalance_item"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_paper_rebalance_item_idempotency"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(20), nullable=False)
    side: Mapped[str] = mapped_column(String(10), nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    price_source: Mapped[str] = mapped_column(String(80), nullable=False)
    price_as_of: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    price_freshness: Mapped[str] = mapped_column(String(20), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    order_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    filled_quantity: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=now_str)
    updated_at: Mapped[str] = mapped_column(String(40), default=now_str)


class BrokerConnection(Base):
    """A broker profile containing only an opaque credential reference."""
    __tablename__ = "broker_connection"
    __table_args__ = (UniqueConstraint("provider", "account_ref", name="uq_broker_provider_account"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    account_ref: Mapped[str] = mapped_column(String(120), nullable=False)
    mode: Mapped[str] = mapped_column(String(20), nullable=False, default="sandbox")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="disabled")
    credential_ref: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=now_str)
    updated_at: Mapped[str] = mapped_column(String(40), default=now_str)


class LiveControl(Base):
    """Durable global live-trading safety control (fail-closed by default)."""
    __tablename__ = "live_control"

    id: Mapped[str] = mapped_column(String(20), primary_key=True, default="global")
    live_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    kill_switch_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    updated_by: Mapped[str] = mapped_column(String(80), default="local-user")
    updated_at: Mapped[str] = mapped_column(String(40), default=now_str)


class LiveOrderDraft(Base):
    """Pre-trade order proposal; it is never an execution receipt."""
    __tablename__ = "live_order_draft"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_live_draft_idempotency"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    connection_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    paper_account_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    code: Mapped[str] = mapped_column(String(20), nullable=False)
    side: Mapped[str] = mapped_column(String(10), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    limit_price: Mapped[float] = mapped_column(Float, nullable=False)
    price_source: Mapped[str] = mapped_column(String(80), nullable=False)
    price_as_of: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    price_freshness: Mapped[str] = mapped_column(String(20), nullable=False)
    notional: Mapped[float] = mapped_column(Float, nullable=False)
    estimated_fee: Mapped[float] = mapped_column(Float, nullable=False)
    cash_before: Mapped[float] = mapped_column(Float, nullable=False)
    cash_after: Mapped[float] = mapped_column(Float, nullable=False)
    risk_status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    risk_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    confirmed_at: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=now_str)
    updated_at: Mapped[str] = mapped_column(String(40), default=now_str)


class AuditEvent(Base):
    """Append-only local audit record with redacted structured details."""
    __tablename__ = "audit_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(String(80), nullable=False, default="local-user")
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(80), nullable=False)
    resource_id: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    details: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[str] = mapped_column(String(40), default=now_str)
