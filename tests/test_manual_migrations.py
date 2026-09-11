"""M2 schema creation and additive SQLite migration checks."""
from sqlalchemy import create_engine, inspect, text

from server.models import database
from server.models import schema as _schema  # noqa: F401  # register ORM tables before init_db


def test_manual_tables_are_created_and_old_sqlite_gets_additive_columns(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy.sqlite3"
    legacy_engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    with legacy_engine.begin() as connection:
        connection.execute(text("CREATE TABLE paper_account (id VARCHAR(36) PRIMARY KEY)"))
    monkeypatch.setattr(database, "engine", legacy_engine)
    database.init_db()

    tables = set(inspect(legacy_engine).get_table_names())
    assert {
        "manual_account", "manual_execution_event", "manual_cash_event",
        "manual_ledger_event", "manual_position_lot", "manual_account_snapshot",
        "manual_position_snapshot", "manual_reconciliation",
    } <= tables
    columns = {item["name"] for item in inspect(legacy_engine).get_columns("paper_account")}
    assert {"market", "validation_only"} <= columns
    legacy_engine.dispose()


def test_h2c_old_holdout_and_job_tables_get_additive_columns_and_preserve_rows(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy-h2c.sqlite3"
    legacy_engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    with legacy_engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE research_holdout_window (
                id VARCHAR(36) PRIMARY KEY, dataset_id VARCHAR(160) NOT NULL,
                data_content_hash VARCHAR(64) NOT NULL, start_date VARCHAR(10) NOT NULL,
                end_date VARCHAR(10) NOT NULL, policy_hash VARCHAR(64) NOT NULL,
                status VARCHAR(20) NOT NULL, created_at VARCHAR(40) NOT NULL,
                opened_at VARCHAR(40), invalidated_at VARCHAR(40)
            )
        """))
        connection.execute(text("""
            CREATE TABLE research_holdout_access (
                id VARCHAR(36) PRIMARY KEY, window_id VARCHAR(36) NOT NULL,
                accessed_at VARCHAR(40) NOT NULL, accessed_by VARCHAR(80) NOT NULL,
                purpose VARCHAR(160) NOT NULL, result_exposed BOOLEAN NOT NULL
            )
        """))
        connection.execute(text("""
            CREATE TABLE manual_daily_job (
                id VARCHAR(36) PRIMARY KEY, job_key VARCHAR(200) NOT NULL,
                account_id VARCHAR(36) NOT NULL, release_id VARCHAR(36), authorization_id VARCHAR(36),
                decision_id VARCHAR(36), run_date VARCHAR(10) NOT NULL, job_type VARCHAR(20) NOT NULL,
                status VARCHAR(20) NOT NULL, attempt_count INTEGER NOT NULL, lease_owner VARCHAR(120),
                lease_until VARCHAR(40), heartbeat_at VARCHAR(40), blocked_reason TEXT,
                result_hash VARCHAR(64), created_at VARCHAR(40) NOT NULL, started_at VARCHAR(40),
                completed_at VARCHAR(40), updated_at VARCHAR(40) NOT NULL
            )
        """))
        connection.execute(text(f"INSERT INTO research_holdout_window VALUES ('w-old','d','{'a' * 64}','2027-01-01','2027-01-02','{'b' * 64}','sealed','2027-01-01T00:00:00',NULL,NULL)"))
        connection.execute(text("INSERT INTO research_holdout_access VALUES ('a-old','w-old','2027-01-01T00:00:00','legacy','old',1)"))
        connection.execute(text("INSERT INTO manual_daily_job (id,job_key,account_id,run_date,job_type,status,attempt_count,created_at,updated_at) VALUES ('j-old','job-old','acct','2027-01-01','review','pending',0,'2027-01-01','2027-01-01')"))
    monkeypatch.setattr(database, "engine", legacy_engine)
    database.init_db()
    database.init_db()

    inspector = inspect(legacy_engine)
    assert {"manual_holdout_binding", "research_holdout_registry_lock"} <= set(inspector.get_table_names())
    assert {"invalidation_reason", "completed_at"} <= {item["name"] for item in inspector.get_columns("research_holdout_window")}
    assert {"binding_hash", "payload_hash"} <= {item["name"] for item in inspector.get_columns("research_holdout_access")}
    assert "lease_token" in {item["name"] for item in inspector.get_columns("manual_daily_job")}
    with legacy_engine.connect() as connection:
        window = connection.execute(text("SELECT invalidation_reason, completed_at FROM research_holdout_window WHERE id='w-old'")).one()
        access = connection.execute(text("SELECT binding_hash, payload_hash FROM research_holdout_access WHERE id='a-old'")).one()
        job = connection.execute(text("SELECT lease_token FROM manual_daily_job WHERE id='j-old'")).one()
        assert window == (None, None)
        assert access == (None, "")
        assert job == (None,)
    legacy_engine.dispose()
