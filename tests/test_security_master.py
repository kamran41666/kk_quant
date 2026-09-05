from datetime import date
from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pandas as pd
import pytest

from quant_engine.data.security_master import SecurityMasterProvider, SecurityMasterUnavailableError
from quant_engine.data.store import MetaDB, StockInfo


def test_security_master_normalizes_codes_boards_and_persists(tmp_path):
    db = MetaDB(db_path=str(tmp_path / "meta.db"))
    frame = pd.DataFrame({
        "full_code": ["000001.SZ", "300750", "688981.SH", "830799.SH", "920002.SH", "900901"],
        "exchange": [None, None, None, None, "SH", "SH"],
        "name": ["平安银行", "宁德时代", "中芯国际", "吉林碳谷", "万达轴承", "B股示例"],
    })
    provider = SecurityMasterProvider(
        fetcher=lambda: frame, db=db, cache_ttl_seconds=60, minimum_snapshot_rows=1
    )

    rows, meta = provider.snapshot()

    assert [row["code"] for row in rows] == [
        "000001.SZ", "300750.SZ", "688981.SH", "830799.BJ", "900901.SH", "920002.BJ"
    ]
    assert {row["board"] for row in rows} == {"主板", "创业板", "科创板", "北交所"}
    assert meta["source"] == "akshare:stock_master"
    assert len(db.get_all_stocks()) == 6


def test_security_master_uses_persisted_cache_when_upstream_fails(tmp_path):
    db = MetaDB(db_path=str(tmp_path / "meta.db"))
    db.upsert_stock(StockInfo(
        code="000001.SZ", name="平安银行", exchange="SZ", board="主板", listed_date=date(1991, 4, 3)
    ))
    provider = SecurityMasterProvider(
        fetcher=lambda: (_ for _ in ()).throw(ConnectionError("upstream down")),
        db=db,
        timeout_seconds=1,
        minimum_snapshot_rows=1,
    )

    rows, meta = provider.snapshot(force=True)

    assert rows[0]["code"] == "000001.SZ"
    assert meta["source"] == "meta:stock_master"
    assert meta["freshness"] == "stale"
    assert provider.health()["status"] == "unavailable"

    _, cached_meta = provider.snapshot()
    assert cached_meta["source"] == "cache:memory"
    assert cached_meta["freshness"] == "stale"


def test_security_master_rejects_small_snapshot_without_local_baseline(tmp_path):
    db = MetaDB(db_path=str(tmp_path / "meta.db"))
    provider = SecurityMasterProvider(
        fetcher=lambda: pd.DataFrame({
            "code": ["000001", "600000"],
            "name": ["平安银行", "浦发银行"],
        }),
        db=db,
    )

    with pytest.raises(SecurityMasterUnavailableError) as raised:
        provider.snapshot(force=True)

    assert raised.value.attempts[0]["source"] == "akshare:stock_master"


def test_security_master_normalizes_legacy_local_exchange_labels(tmp_path):
    db = MetaDB(db_path=str(tmp_path / "meta.db"))
    db.upsert_stocks([
        StockInfo(code="000001", name="平安银行", exchange="SZSE", board="主板", listed_date=None),
        StockInfo(code="920002", name="万达轴承", exchange="SH", board="主板", listed_date=None),
    ])
    provider = SecurityMasterProvider(
        fetcher=lambda: (_ for _ in ()).throw(ConnectionError("upstream down")),
        db=db,
        timeout_seconds=1,
        minimum_snapshot_rows=1,
    )

    rows, _ = provider.snapshot(force=True)

    assert {row["code"] for row in rows} == {"000001.SZ", "920002.BJ"}
    assert {row["board"] for row in rows} == {"主板", "北交所"}


def test_security_master_success_replaces_removed_local_symbols(tmp_path):
    db = MetaDB(db_path=str(tmp_path / "meta.db"))
    db.upsert_stock(StockInfo(
        code="000999.SZ", name="已退市示例", exchange="SZ", board="主板", listed_date=None
    ))
    provider = SecurityMasterProvider(
        fetcher=lambda: pd.DataFrame({"code": ["000001"], "name": ["平安银行"]}),
        db=db,
        minimum_snapshot_rows=1,
    )

    provider.snapshot(force=True)

    assert [stock.code for stock in db.get_all_stocks()] == ["000001.SZ"]


def test_security_master_rejects_truncated_success_and_keeps_cache(tmp_path):
    db = MetaDB(db_path=str(tmp_path / "meta.db"))
    db.upsert_stocks([
        StockInfo(code="000001.SZ", name="平安银行", exchange="SZ", board="主板", listed_date=None),
        StockInfo(code="000002.SZ", name="万科A", exchange="SZ", board="主板", listed_date=None),
        StockInfo(code="600000.SH", name="浦发银行", exchange="SH", board="主板", listed_date=None),
        StockInfo(code="600519.SH", name="贵州茅台", exchange="SH", board="主板", listed_date=None),
        StockInfo(code="300750.SZ", name="宁德时代", exchange="SZ", board="创业板", listed_date=None),
    ])
    provider = SecurityMasterProvider(
        fetcher=lambda: pd.DataFrame({"code": ["000001"], "name": ["平安银行"]}),
        db=db,
        timeout_seconds=1,
        minimum_snapshot_rows=1,
    )

    rows, meta = provider.snapshot(force=True)

    assert meta["source"] == "meta:stock_master"
    assert meta["freshness"] == "stale"
    assert len(rows) == 5
    assert len(db.get_all_stocks()) == 5


def test_security_master_timeout_enters_cooldown(tmp_path):
    db = MetaDB(db_path=str(tmp_path / "meta.db"))
    db.upsert_stock(StockInfo(
        code="000001.SZ", name="平安银行", exchange="SZ", board="主板", listed_date=None
    ))
    release = threading.Event()

    def blocked_fetcher():
        release.wait(5)
        return pd.DataFrame({"code": ["000001"], "name": ["平安银行"]})

    provider = SecurityMasterProvider(
        fetcher=blocked_fetcher, db=db, timeout_seconds=0.02,
        minimum_snapshot_rows=1,
    )
    try:
        first, first_meta = provider.snapshot(force=True)
        started = time.monotonic()
        second, second_meta = provider.snapshot(force=True)
        elapsed = time.monotonic() - started
    finally:
        release.set()
        provider.close()

    assert first_meta["freshness"] == "stale"
    assert second_meta["freshness"] == "stale"
    assert first[0]["code"] == second[0]["code"] == "000001.SZ"
    assert elapsed < 0.5


def test_security_master_refresh_is_single_flight(tmp_path):
    db = MetaDB(db_path=str(tmp_path / "meta.db"))
    calls = 0
    calls_lock = threading.Lock()

    def fetcher():
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.05)
        return pd.DataFrame({"code": ["000001"], "name": ["平安银行"]})

    provider = SecurityMasterProvider(
        fetcher=fetcher, db=db, minimum_snapshot_rows=1
    )
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _: provider.snapshot(), range(2)))

    assert calls == 1
    assert all(result[0][0]["code"] == "000001.SZ" for result in results)
