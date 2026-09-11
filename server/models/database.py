"""SQLAlchemy engine + session factory"""
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from server.config import settings

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False},
    echo=False,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def ensure_savepoint_transaction(db) -> None:
    """Ensure SQLite has a real outer transaction before ``begin_nested``.

    Python's sqlite legacy transaction mode does not begin a transaction for a
    read, and releasing the first savepoint can otherwise commit unexpectedly.
    The explicit immediate transaction also gives callers a database-level
    write boundary for atomic manual-domain operations.
    """
    if db.get_bind().dialect.name != "sqlite":
        return
    connection = db.connection()
    raw = connection.connection.driver_connection
    if not raw.in_transaction:
        connection.exec_driver_sql("BEGIN IMMEDIATE")


def init_db():
    Base.metadata.create_all(bind=engine)
    # The project intentionally has no migration dependency in Phase 2. Keep
    # the local SQLite database forward-compatible when new paper fields are
    # added between releases. CREATE TABLE handles new tables; these additive
    # columns handle databases created by an earlier build.
    if engine.dialect.name != "sqlite":
        return
    additive = {
        "paper_account": {
            "market": "VARCHAR(20) NOT NULL DEFAULT 'a-share'",
            "validation_only": "BOOLEAN NOT NULL DEFAULT 0",
        },
        "paper_position": {
            "unlock_date": "VARCHAR(10)",
            "locked_lots": "TEXT NOT NULL DEFAULT '[]'",
        },
        "factor_experiment": {
            "stage": "VARCHAR(20) NOT NULL DEFAULT 'training'",
            "evaluation_policy": "TEXT NOT NULL DEFAULT '{}'",
            "label_spec": "TEXT NOT NULL DEFAULT '{}'",
        },
        "paper_account_position": {
            "market": "VARCHAR(20) NOT NULL DEFAULT 'a-share'",
            "last_price_source": "VARCHAR(80) NOT NULL DEFAULT 'manual_input'",
            "last_price_as_of": "VARCHAR(40)",
            "last_price_freshness": "VARCHAR(20) NOT NULL DEFAULT 'manual'",
        },
        "paper_lot": {
            "market": "VARCHAR(20) NOT NULL DEFAULT 'a-share'",
            "owner": "VARCHAR(20) NOT NULL DEFAULT 'manual'",
            "owner_id": "VARCHAR(36)",
        },
        "paper_order": {
            "market": "VARCHAR(20) NOT NULL DEFAULT 'a-share'",
            "price_source": "VARCHAR(80) NOT NULL DEFAULT 'manual_input'",
            "price_as_of": "VARCHAR(40)",
            "price_freshness": "VARCHAR(20) NOT NULL DEFAULT 'manual'",
        },
        "paper_scheduler_run": {
            "last_run_at": "VARCHAR(40) NOT NULL DEFAULT ''",
        },
        "paper_valuation": {
            "price_metadata": "TEXT NOT NULL DEFAULT '{}'",
        },
        "strategy_observation": {
            "managed_codes": "TEXT NOT NULL DEFAULT '[]'",
            "backtest_run_id": "VARCHAR(36)",
            "market": "VARCHAR(20)",
            "strategy_fingerprint": "VARCHAR(64)",
            "pending_signals": "TEXT NOT NULL DEFAULT '{}'",
            "pending_signal_date": "VARCHAR(10)",
            "purpose": "VARCHAR(32) NOT NULL DEFAULT 'research'",
        },
        "paper_fill": {
            "market": "VARCHAR(20) NOT NULL DEFAULT 'a-share'",
        },
        "run": {
            "market": "VARCHAR(20)",
            "strategy_fingerprint": "VARCHAR(64)",
            "data_manifest": "TEXT",
            "data_end": "VARCHAR(10)",
            "calendar_version": "VARCHAR(80)",
            "execution_model": "VARCHAR(80)",
            "eligible_for_observation": "BOOLEAN NOT NULL DEFAULT 0",
        },
        "paper_rebalance_plan": {
            "lease_owner": "VARCHAR(80)",
            "lease_until": "VARCHAR(40)",
        },
        "strategy": {
            "market": "VARCHAR(20) NOT NULL DEFAULT 'a-share'",
        },
        "manual_account": {
            "create_idempotency_key": "VARCHAR(160)",
        },
        "strategy_promotion_evaluation": {
            "previous_evaluation_id": "VARCHAR(36)",
            "previous_evaluation_hash": "VARCHAR(64) NOT NULL DEFAULT ''",
            "evaluation_hash": "VARCHAR(64) NOT NULL DEFAULT ''",
        },
        "research_holdout_window": {
            "invalidation_reason": "TEXT",
            "completed_at": "VARCHAR(40)",
        },
        "research_holdout_access": {
            "binding_hash": "VARCHAR(64)",
            "payload_hash": "VARCHAR(64) NOT NULL DEFAULT ''",
        },
        "manual_daily_job": {
            "lease_token": "VARCHAR(64)",
        },
    }
    inspector = inspect(engine)
    with engine.begin() as connection:
        for table, columns in additive.items():
            if table not in inspector.get_table_names():
                continue
            present = {item["name"] for item in inspector.get_columns(table)}
            for column, definition in columns.items():
                if column not in present:
                    connection.execute(text(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {definition}'))
