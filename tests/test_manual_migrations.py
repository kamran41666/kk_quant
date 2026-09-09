"""M2 schema creation and additive SQLite migration checks."""
from sqlalchemy import create_engine, inspect, text

from server.models import database


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
