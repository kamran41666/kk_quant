"""ORM models for server database"""
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional
import uuid
from sqlalchemy import Boolean, String, Float, Integer, Text, Numeric, CheckConstraint, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from server.models.database import Base


def uuid4_str() -> str:
    return uuid.uuid4().hex[:12]


def now_str() -> str:
    return datetime.now().isoformat()


def manual_now_str() -> str:
    """UTC timestamp used by the append-only manual execution chain."""
    return datetime.now(timezone.utc).isoformat()


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


class FactorCandidate(Base):
    """A deduplicated restricted factor proposal; expression_spec is canonical JSON."""
    __tablename__ = "factor_candidate"
    __table_args__ = (
        UniqueConstraint("expression_hash", name="uq_factor_candidate_expression_hash"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    expression_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    expression_spec: Mapped[str] = mapped_column(Text, nullable=False)
    hypothesis: Mapped[str] = mapped_column(Text, nullable=False)
    direction: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    source: Mapped[str] = mapped_column(String(120), nullable=False)
    parent_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="registered")
    rejection_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=now_str)
    updated_at: Mapped[str] = mapped_column(String(40), default=now_str)


class FactorExperiment(Base):
    """Persistent queue record for one candidate/data/protocol evaluation."""
    __tablename__ = "factor_experiment"
    __table_args__ = (
        UniqueConstraint(
            "candidate_id", "dataset_id", "start_date", "end_date",
            "forward_horizon", name="uq_factor_experiment_identity",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    candidate_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    dataset_id: Mapped[str] = mapped_column(String(160), nullable=False)
    data_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    start_date: Mapped[str] = mapped_column(String(10), nullable=False)
    end_date: Mapped[str] = mapped_column(String(10), nullable=False)
    forward_horizon: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    stage: Mapped[str] = mapped_column(String(20), nullable=False, default="training")
    evaluation_policy: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    # Full forward-label identity; legacy rows default to the prior open/open
    # convention and are not silently reinterpreted as manual-daily labels.
    label_spec: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="queued", index=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_owner: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    lease_until: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    result_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    artifact_dir: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error_code: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=now_str)
    started_at: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    completed_at: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    updated_at: Mapped[str] = mapped_column(String(40), default=now_str)


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
    # Legacy single-account snapshots now retain enough lot state to enforce
    # T+1 after a process restart. Nullable unlock_date keeps older SQLite
    # files readable; locked_lots is a JSON list of {unlock_date, shares}.
    unlock_date: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    locked_lots: Mapped[str] = mapped_column(Text, nullable=False, default="[]")


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
    validation_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
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
    # ``research`` requires a passed research gate. ``engineering_validation``
    # is an explicitly non-promotable paper rehearsal.
    purpose: Mapped[str] = mapped_column(String(32), nullable=False, default="research")
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


class ManualAccount(Base):
    """A human-operated account; it has no broker connection or submit path."""
    __tablename__ = "manual_account"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    create_idempotency_key: Mapped[Optional[str]] = mapped_column(String(160), nullable=True, unique=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="CNY")
    broker_label: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    confirmed_cash: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False, default=Decimal("0"))
    ledger_checkpoint_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    risk_policy: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    last_reconciled_at: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=manual_now_str)
    updated_at: Mapped[str] = mapped_column(String(40), default=manual_now_str)

    __table_args__ = (
        CheckConstraint("currency = 'CNY'", name="ck_manual_account_currency_cny"),
        CheckConstraint("confirmed_cash >= 0", name="ck_manual_account_cash_nonnegative"),
        CheckConstraint("status IN ('draft', 'active', 'reconcile', 'suspended', 'closed')", name="ck_manual_account_status"),
    )


class StrategyRelease(Base):
    """Immutable release envelope; qualification never comes from Paper* rows."""
    __tablename__ = "strategy_release"
    __table_args__ = (
        UniqueConstraint("strategy_key", "version", name="uq_strategy_release_key_version"),
        UniqueConstraint("release_hash", name="uq_strategy_release_hash"),
        CheckConstraint("market = 'a-share'", name="ck_strategy_release_market"),
        CheckConstraint(
            "status IN ('draft', 'research_blocked', 'research_passed', 'portfolio_passed', 'holdout_passed', 'paper_observing', 'paper_passed', 'manual_ready', 'suspended', 'retired')",
            name="ck_strategy_release_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    version: Mapped[str] = mapped_column(String(40), nullable=False)
    strategy_key: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    bundle_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    release_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    market: Mapped[str] = mapped_column(String(20), nullable=False, default="a-share")
    research_evidence: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    execution_policy: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    risk_policy: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    promotion_policy: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="draft")
    approved_by: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    approved_at: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=manual_now_str)
    updated_at: Mapped[str] = mapped_column(String(40), default=manual_now_str)


class ManualExecutionAuthorization(Base):
    """User/account-specific limits; it is not a broker authorization token."""
    __tablename__ = "manual_execution_authorization"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'approved', 'active', 'reconcile', 'suspended', 'revoked', 'expired')",
            name="ck_manual_authorization_status",
        ),
        CheckConstraint("capital_limit > 0 AND max_order_notional > 0", name="ck_manual_authorization_money_positive"),
        CheckConstraint("max_gross_exposure > 0 AND max_gross_exposure <= 1", name="ck_manual_authorization_exposure"),
        CheckConstraint("max_single_weight > 0 AND max_single_weight <= 1", name="ck_manual_authorization_weight"),
        CheckConstraint("max_daily_items > 0", name="ck_manual_authorization_items_positive"),
        CheckConstraint("max_daily_loss > 0 AND max_drawdown > 0", name="ck_manual_authorization_loss_positive"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    release_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    account_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    capital_limit: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    max_order_notional: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    max_gross_exposure: Mapped[Decimal] = mapped_column(Numeric(12, 8), nullable=False)
    max_single_weight: Mapped[Decimal] = mapped_column(Numeric(12, 8), nullable=False)
    max_daily_items: Mapped[int] = mapped_column(Integer, nullable=False)
    max_daily_loss: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    max_drawdown: Mapped[Decimal] = mapped_column(Numeric(12, 8), nullable=False)
    revocation_policy: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    revocation_policy_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    valid_from: Mapped[str] = mapped_column(String(40), nullable=False)
    valid_until: Mapped[str] = mapped_column(String(40), nullable=False)
    first_fill_event_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    first_fill_at: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    approved_by: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    approved_at: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    revoked_at: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=manual_now_str)
    updated_at: Mapped[str] = mapped_column(String(40), default=manual_now_str)


class ResearchHoldoutWindow(Base):
    """Sealed holdout lifecycle; opening is itself an append-only access fact."""
    __tablename__ = "research_holdout_window"
    __table_args__ = (
        CheckConstraint("status IN ('sealed', 'opened', 'invalidated', 'completed')", name="ck_holdout_status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    dataset_id: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    data_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    start_date: Mapped[str] = mapped_column(String(10), nullable=False)
    end_date: Mapped[str] = mapped_column(String(10), nullable=False)
    policy_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="sealed")
    created_at: Mapped[str] = mapped_column(String(40), default=manual_now_str)
    opened_at: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    invalidated_at: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)


class ResearchHoldoutAccess(Base):
    """Append-only record of every holdout open/read attempt."""
    __tablename__ = "research_holdout_access"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    window_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    accessed_at: Mapped[str] = mapped_column(String(40), default=manual_now_str)
    accessed_by: Mapped[str] = mapped_column(String(80), nullable=False)
    purpose: Mapped[str] = mapped_column(String(160), nullable=False)
    result_exposed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class DailyDecision(Base):
    """Immutable daily signal decision bound to one release and authorization."""
    __tablename__ = "daily_decision"
    __table_args__ = (
        UniqueConstraint("authorization_id", "signal_date", "release_id", "revision", name="uq_manual_daily_decision_revision"),
        UniqueConstraint("decision_hash", name="uq_manual_daily_decision_hash"),
        CheckConstraint("action IN ('hold', 'rebalance', 'reduce', 'flat', 'blocked', 'reconcile')", name="ck_manual_decision_action"),
        CheckConstraint("status IN ('draft', 'ready', 'blocked', 'superseded', 'reviewed')", name="ck_manual_decision_status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    release_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    authorization_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    signal_date: Mapped[str] = mapped_column(String(10), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    data_as_of: Mapped[str] = mapped_column(String(40), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    decision_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    risk_state: Mapped[str] = mapped_column(String(30), nullable=False, default="normal")
    target_weights: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    reason_codes: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    blocked_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    supersedes_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    created_at: Mapped[str] = mapped_column(String(40), default=manual_now_str)


class ManualCohort(Base):
    """One of the two rolling manual sleeves for a daily decision."""
    __tablename__ = "manual_cohort"
    __table_args__ = (
        UniqueConstraint("authorization_id", "signal_date", "sleeve_index", name="uq_manual_cohort_sleeve"),
        CheckConstraint("sleeve_index IN (0, 1)", name="ck_manual_cohort_sleeve"),
        CheckConstraint("status IN ('planned', 'entering', 'open', 'exiting', 'closed', 'blocked', 'cancelled')", name="ck_manual_cohort_status"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    authorization_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    decision_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    signal_date: Mapped[str] = mapped_column(String(10), nullable=False)
    planned_entry_date: Mapped[str] = mapped_column(String(10), nullable=False)
    planned_exit_date: Mapped[str] = mapped_column(String(10), nullable=False)
    sleeve_index: Mapped[int] = mapped_column(Integer, nullable=False)
    budget: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="planned")
    created_at: Mapped[str] = mapped_column(String(40), default=manual_now_str)
    closed_at: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)


class ManualExecutionPlan(Base):
    """Versioned human-readable order list; never an order submission."""
    __tablename__ = "manual_execution_plan"
    __table_args__ = (
        UniqueConstraint("authorization_id", "execution_date", "execution_session", "plan_type", "version", name="uq_manual_plan_version"),
        UniqueConstraint("idempotency_key", name="uq_manual_plan_idempotency"),
        UniqueConstraint("plan_hash", name="uq_manual_plan_hash"),
        CheckConstraint("execution_session IN ('open', 'close')", name="ck_manual_plan_session"),
        CheckConstraint("plan_type IN ('entry', 'exit', 'rebalance', 'risk', 'mixed')", name="ck_manual_plan_type"),
        CheckConstraint("status IN ('draft', 'ready', 'viewed', 'partially_filled', 'completed', 'expired', 'cancelled', 'blocked', 'superseded')", name="ck_manual_plan_status"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    decision_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    authorization_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    account_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    execution_date: Mapped[str] = mapped_column(String(10), nullable=False)
    execution_session: Mapped[str] = mapped_column(String(10), nullable=False)
    plan_type: Mapped[str] = mapped_column(String(20), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    account_snapshot_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    quote_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    authorization_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    trading_rule_id: Mapped[str] = mapped_column(String(80), nullable=False)
    trading_rule_version: Mapped[str] = mapped_column(String(40), nullable=False)
    trading_rule_effective_date: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    cash_before: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    expected_cash_after: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    expected_fees: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False, default=Decimal("0"))
    blocked_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    expires_at: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    viewed_at: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    supersedes_plan_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=manual_now_str)
    updated_at: Mapped[str] = mapped_column(String(40), default=manual_now_str)


class ManualExecutionItem(Base):
    """One planned human action; it is not an execution receipt."""
    __tablename__ = "manual_execution_item"
    __table_args__ = (
        UniqueConstraint("plan_id", "cohort_id", "code", "side", name="uq_manual_plan_cohort_code_side"),
        UniqueConstraint("idempotency_key", name="uq_manual_item_idempotency"),
        CheckConstraint("side IN ('buy', 'sell')", name="ck_manual_item_side"),
        CheckConstraint("phase IN ('sell', 'buy')", name="ck_manual_item_phase"),
        CheckConstraint("cash_dependency_type = 'confirmed_cash' OR cash_dependency_type IS NULL", name="ck_manual_item_cash_dependency"),
        CheckConstraint("status IN ('planned', 'submitted', 'cancelled', 'skipped', 'partially_filled', 'filled', 'unfilled', 'rejected', 'expired', 'blocked')", name="ck_manual_item_status"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    plan_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    cohort_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    code: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    phase: Mapped[str] = mapped_column(String(8), nullable=False)
    pre_quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, default=Decimal("0"))
    available_quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, default=Decimal("0"))
    available_cash_before: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False, default=Decimal("0"))
    target_quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    target_weight: Mapped[Decimal] = mapped_column(Numeric(12, 8), nullable=False, default=Decimal("0"))
    planned_quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    cash_required: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False, default=Decimal("0"))
    cash_dependency_type: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    depends_on_item_ids: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    reference_price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    price_source: Mapped[str] = mapped_column(String(80), nullable=False)
    price_as_of: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    min_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8), nullable=True)
    max_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8), nullable=True)
    expected_notional: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False, default=Decimal("0"))
    estimated_commission: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False, default=Decimal("0"))
    estimated_tax: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False, default=Decimal("0"))
    estimated_other_fee: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False, default=Decimal("0"))
    order_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    reason_codes: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="planned")
    confirmed_quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, default=Decimal("0"))
    created_at: Mapped[str] = mapped_column(String(40), default=manual_now_str)
    updated_at: Mapped[str] = mapped_column(String(40), default=manual_now_str)


class ManualExecutionEvent(Base):
    """An immutable user-reported execution fact, never a broker submission."""
    __tablename__ = "manual_execution_event"
    __table_args__ = (
        UniqueConstraint("client_event_id", name="uq_manual_execution_client_event"),
        UniqueConstraint("account_id", "user_trade_ref", name="uq_manual_execution_trade_ref"),
        CheckConstraint("source = 'user_reported'", name="ck_manual_execution_source"),
        CheckConstraint(
            "event_type IN ('submitted', 'partial_fill', 'fill', 'cancelled', 'rejected', 'skipped', 'fee_adjustment', 'fill_reversal', 'fill_correction')",
            name="ck_manual_execution_event_type",
        ),
        CheckConstraint("side IN ('buy', 'sell') OR side IS NULL", name="ck_manual_execution_side"),
        CheckConstraint("quantity IS NULL OR quantity > 0", name="ck_manual_execution_quantity_positive"),
        CheckConstraint("price IS NULL OR price > 0", name="ck_manual_execution_price_positive"),
        CheckConstraint("commission >= 0 AND stamp_duty >= 0 AND other_fee >= 0 AND total_fee >= 0", name="ck_manual_execution_fees_nonnegative"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    client_event_id: Mapped[str] = mapped_column(String(160), nullable=False)
    item_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    account_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    code: Mapped[Optional[str]] = mapped_column(String(20), nullable=True, index=True)
    cohort_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    user_trade_ref: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    quantity: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8), nullable=True)
    price: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8), nullable=True)
    commission: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False, default=Decimal("0"))
    stamp_duty: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False, default=Decimal("0"))
    other_fee: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False, default=Decimal("0"))
    total_fee: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False, default=Decimal("0"))
    traded_at: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="user_reported")
    supersedes_event_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), default=manual_now_str)


class ManualCashEvent(Base):
    """Append-only cash-flow fact used to establish and correct cash."""
    __tablename__ = "manual_cash_event"
    __table_args__ = (
        UniqueConstraint("account_id", "idempotency_key", name="uq_manual_cash_idempotency"),
        CheckConstraint("source = 'user_reported'", name="ck_manual_cash_source"),
        CheckConstraint(
            "event_type IN ('opening_balance', 'deposit', 'withdrawal', 'interest', 'fee_adjustment')",
            name="ck_manual_cash_event_type",
        ),
        CheckConstraint("amount <> 0", name="ck_manual_cash_amount_nonzero"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    account_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    occurred_at: Mapped[str] = mapped_column(String(40), nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="user_reported")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="confirmed")
    correction_of: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=manual_now_str)


class ManualLedgerEvent(Base):
    """Per-account hash-chained economic event, rebuilt from source facts."""
    __tablename__ = "manual_ledger_event"
    __table_args__ = (
        UniqueConstraint("account_id", "account_sequence", name="uq_manual_ledger_account_sequence"),
        CheckConstraint("cash_delta <> 0 OR quantity_delta <> 0", name="ck_manual_ledger_nonempty"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    account_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    account_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    reference_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    trade_date: Mapped[str] = mapped_column(String(10), nullable=False)
    cash_delta: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False, default=Decimal("0"))
    quantity_delta: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, default=Decimal("0"))
    code: Mapped[Optional[str]] = mapped_column(String(20), nullable=True, index=True)
    cohort_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    payload: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    previous_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    event_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), default=manual_now_str)


class ManualPositionLot(Base):
    """Materialized A-share lots; source-of-truth remains the ledger."""
    __tablename__ = "manual_position_lot"
    __table_args__ = (
        CheckConstraint("quantity > 0 AND remaining_quantity >= 0 AND remaining_quantity <= quantity", name="ck_manual_lot_quantity"),
        CheckConstraint("avg_cost >= 0", name="ck_manual_lot_cost_nonnegative"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    account_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    cohort_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    remaining_quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    avg_cost: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    buy_at: Mapped[str] = mapped_column(String(10), nullable=False)
    unlock_date: Mapped[str] = mapped_column(String(10), nullable=False)
    planned_exit_date: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open")
    created_at: Mapped[str] = mapped_column(String(40), default=manual_now_str)
    updated_at: Mapped[str] = mapped_column(String(40), default=manual_now_str)


class ManualAccountSnapshot(Base):
    """User-reported broker statement snapshot, separate from the ledger."""
    __tablename__ = "manual_account_snapshot"
    __table_args__ = (
        UniqueConstraint("account_id", "idempotency_key", name="uq_manual_snapshot_idempotency"),
        CheckConstraint("source = 'user_reported'", name="ck_manual_snapshot_source"),
        CheckConstraint("status IN ('pending', 'reconciled', 'different')", name="ck_manual_snapshot_status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    account_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    as_of: Mapped[str] = mapped_column(String(40), nullable=False)
    cash: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    total_asset: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="user_reported")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=manual_now_str)


class ManualPositionSnapshot(Base):
    """Positions copied from a user-reported account snapshot."""
    __tablename__ = "manual_position_snapshot"
    __table_args__ = (
        CheckConstraint("total_quantity >= 0 AND available_quantity >= 0", name="ck_manual_snapshot_quantity"),
        CheckConstraint("avg_cost >= 0 AND market_value >= 0", name="ck_manual_snapshot_values"),
    )

    snapshot_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    code: Mapped[str] = mapped_column(String(20), primary_key=True)
    total_quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    available_quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    avg_cost: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    market_value: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)


class ManualReconciliation(Base):
    """Durable comparison between the ledger and one user-reported snapshot."""
    __tablename__ = "manual_reconciliation"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'matched', 'different', 'resolved')", name="ck_manual_reconciliation_status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    account_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    plan_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    snapshot_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    planned_quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, default=Decimal("0"))
    filled_quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, default=Decimal("0"))
    unfilled_quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, default=Decimal("0"))
    quantity_differences: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    position_differences: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    cash_difference: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False, default=Decimal("0"))
    reference_slippage: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False, default=Decimal("0"))
    actual_fees: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False, default=Decimal("0"))
    ledger_checkpoint_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    calculated_at: Mapped[str] = mapped_column(String(40), default=manual_now_str)
    resolved_at: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)


class ManualDailyJob(Base):
    """Durable, idempotent work item for the manual daily cycle."""
    __tablename__ = "manual_daily_job"
    __table_args__ = (
        UniqueConstraint("job_key", name="uq_manual_daily_job_key"),
        CheckConstraint("job_type IN ('open_plan', 'close_plan', 'valuation', 'reconcile', 'review')", name="ck_manual_daily_job_type"),
        CheckConstraint("status IN ('pending', 'leased', 'running', 'succeeded', 'blocked', 'failed')", name="ck_manual_daily_job_status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    job_key: Mapped[str] = mapped_column(String(200), nullable=False)
    account_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    release_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    authorization_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    decision_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    run_date: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    job_type: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_owner: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    lease_until: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    heartbeat_at: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    blocked_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    result_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=manual_now_str)
    started_at: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    completed_at: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    updated_at: Mapped[str] = mapped_column(String(40), default=manual_now_str)


class ManualBackupRecord(Base):
    """Content-addressed metadata for a local manual-table backup."""
    __tablename__ = "manual_backup_record"
    __table_args__ = (
        UniqueConstraint("content_hash", name="uq_manual_backup_hash"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    account_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    backup_path: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(30), nullable=False, default="manual-backup-v1")
    row_counts: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="written")
    created_at: Mapped[str] = mapped_column(String(40), default=manual_now_str)


class ManualValuation(Base):
    """User-reported end-of-day valuation; never a broker quote."""
    __tablename__ = "manual_valuation"
    __table_args__ = (
        UniqueConstraint("account_id", "valuation_date", name="uq_manual_valuation_date"),
        CheckConstraint("cash >= 0 AND market_value >= 0 AND total_asset >= 0", name="ck_manual_valuation_nonnegative"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    account_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    valuation_date: Mapped[str] = mapped_column(String(10), nullable=False)
    cash: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    market_value: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    total_asset: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    daily_return: Mapped[Decimal] = mapped_column(Numeric(18, 10), nullable=False, default=Decimal("0"))
    external_cash_flow: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False, default=Decimal("0"))
    pnl: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False, default=Decimal("0"))
    price_source: Mapped[str] = mapped_column(String(80), nullable=False, default="user_reported")
    price_as_of: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    price_freshness: Mapped[str] = mapped_column(String(20), nullable=False, default="user_reported")
    ledger_checkpoint_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    created_at: Mapped[str] = mapped_column(String(40), default=manual_now_str)


class ManualDailyReview(Base):
    """Cash-flow-neutral daily review and execution deviation snapshot."""
    __tablename__ = "manual_daily_review"
    __table_args__ = (
        UniqueConstraint("account_id", "review_date", name="uq_manual_daily_review_date"),
        CheckConstraint("status IN ('draft', 'ready', 'blocked', 'superseded')", name="ck_manual_daily_review_status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    account_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    review_date: Mapped[str] = mapped_column(String(10), nullable=False)
    valuation_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    reconciliation_status: Mapped[str] = mapped_column(String(20), nullable=False, default="not_run")
    planned_item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reported_fill_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unfilled_item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    execution_deviation: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False, default=Decimal("0"))
    factor_decay_status: Mapped[str] = mapped_column(String(30), nullable=False, default="not_evaluated")
    data_health: Mapped[str] = mapped_column(String(30), nullable=False, default="user_reported")
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    review_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[str] = mapped_column(String(40), default=manual_now_str)


class ResearchRevision(Base):
    """Research-only revision queue item; it cannot change a manual release."""
    __tablename__ = "research_revision"
    __table_args__ = (
        CheckConstraint("status IN ('queued', 'in_review', 'accepted', 'rejected')", name="ck_research_revision_status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    release_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    account_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    review_date: Mapped[str] = mapped_column(String(10), nullable=False)
    reason_codes: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    evidence: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    revision_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    created_at: Mapped[str] = mapped_column(String(40), default=manual_now_str)


class ManualCorporateActionFact(Base):
    """Immutable user-reported corporate-action fact; no automatic adjustment."""
    __tablename__ = "manual_corporate_action_fact"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_manual_corporate_action_key"),
        CheckConstraint("source = 'user_reported'", name="ck_manual_corporate_action_source"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    account_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    action_type: Mapped[str] = mapped_column(String(40), nullable=False)
    effective_date: Mapped[str] = mapped_column(String(10), nullable=False)
    factor: Mapped[Decimal] = mapped_column(Numeric(20, 10), nullable=False, default=Decimal("1"))
    cash_amount: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False, default=Decimal("0"))
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="user_reported")
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=manual_now_str)
