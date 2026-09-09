import pytest
from datetime import date
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from server.models.database import Base


@pytest.fixture
def sample_dates():
    """已知的 A 股交易日/非交易日样本 (手动验证过的)"""
    return {
        "trading": [date(2024, 1, 2), date(2024, 1, 3), date(2024, 12, 31)],
        "holiday": [date(2024, 1, 1), date(2024, 2, 12)],
        "weekend": [date(2024, 1, 6), date(2024, 1, 7)],
    }


@pytest.fixture
def db_session():
    """Fresh SQLite session shared by database-backed manual route tests."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    yield session
    session.close()
    Base.metadata.drop_all(bind=engine)
