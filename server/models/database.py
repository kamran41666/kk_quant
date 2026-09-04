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


def init_db():
    Base.metadata.create_all(bind=engine)
    # The project intentionally has no migration dependency in Phase 2. Keep
    # the local SQLite database forward-compatible when new paper fields are
    # added between releases. CREATE TABLE handles new tables; these additive
    # columns handle databases created by an earlier build.
    if engine.dialect.name != "sqlite":
        return
    additive = {
        "paper_order": {
            "price_source": "VARCHAR(80) NOT NULL DEFAULT 'manual_input'",
            "price_as_of": "VARCHAR(40)",
            "price_freshness": "VARCHAR(20) NOT NULL DEFAULT 'manual'",
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
