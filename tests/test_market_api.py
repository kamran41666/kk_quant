from datetime import date, datetime, timezone, timedelta
import sqlite3
import time

from fastapi import HTTPException
import pandas as pd
import pytest

from quant_engine.data.live import MarketDataUnavailableError, MarketQuote
from server.api import market


class StubProvider:
    def __init__(self, quotes=None, error=None):
        self.quotes = quotes or []
        self.error = error

    def fetch_quotes(self, codes=None):
        if self.error:
            raise self.error
        if codes is None:
            return self.quotes
        wanted = set(codes)
        return [quote for quote in self.quotes if quote.code in wanted]

    def health(self):
        return [
            {"name": "akshare:eastmoney", "status": "unavailable"},
            {"name": "akshare:sina", "status": "ok"},
        ]


def quote(code="000001.SZ", fallback=False):
    now = datetime.now(timezone.utc).isoformat()
    return MarketQuote(
        code=code,
        name="平安银行",
        price=10.5,
        change_pct=1.2,
        volume=1000.0,
        amount=10000.0,
        source="akshare:sina" if fallback else "akshare:eastmoney",
        as_of=now,
        received_at=now,
        freshness="fresh",
        is_fallback=fallback,
    )


def test_quotes_response_contract(monkeypatch):
    monkeypatch.setattr(market, "live_market_provider", StubProvider([quote(fallback=True)]))

    response = market.get_quotes("000001.SZ")

    assert response["data"][0]["code"] == "000001.SZ"
    assert response["data"][0]["source"] == "akshare:sina"
    assert response["data"][0]["is_fallback"] is True
    assert response["meta"]["fallback_used"] is True
    assert response["meta"]["status"] == "ok"


def test_quotes_rejects_non_canonical_codes():
    try:
        market.get_quotes("000001,not-a-code")
        assert False, "expected HTTPException"
    except HTTPException as exc:
        assert exc.status_code == 422
        assert exc.detail["code"] == "INVALID_STOCK_CODE"


def test_quotes_returns_503_without_fabricated_data(monkeypatch):
    unavailable = MarketDataUnavailableError([
        {"source": "akshare:eastmoney", "error": "TimeoutError: timeout"},
        {"source": "akshare:sina", "error": "TimeoutError: timeout"},
    ])
    monkeypatch.setattr(market, "live_market_provider", StubProvider(error=unavailable))

    try:
        market.get_quotes("000001.SZ")
        assert False, "expected HTTPException"
    except HTTPException as exc:
        assert exc.status_code == 503
        assert exc.detail["code"] == "LIVE_MARKET_DATA_UNAVAILABLE"
        assert "data" not in exc.detail


def test_quotes_shards_large_requests_to_bound_provider_payload(monkeypatch):
    requested = [f"{index:06d}.SZ" for index in range(1, 206)]
    available = [MarketQuote(**{**quote(code).to_dict(), "code": code}) for code in requested]

    class RecordingProvider(StubProvider):
        def __init__(self):
            super().__init__(available)
            self.calls = []

        def fetch_quotes(self, codes=None):
            self.calls.append(list(codes or []))
            return super().fetch_quotes(codes)

    provider = RecordingProvider()
    monkeypatch.setattr(market, "live_market_provider", provider)

    response = market.get_quotes(",".join(requested))

    assert [len(batch) for batch in provider.calls] == [100, 100, 5]
    assert response["meta"]["requested_count"] == 205
    assert response["meta"]["returned_count"] == 205
    assert response["meta"]["status"] == "ok"


def test_quotes_rejects_requests_over_public_limit():
    requested = ",".join(f"{index:06d}.SZ" for index in range(1, 502))

    with pytest.raises(HTTPException) as raised:
        market.get_quotes(requested)

    assert raised.value.status_code == 422
    assert "At most 500" in str(raised.value.detail)


def test_daily_coverage_endpoint_returns_machine_readable_report(monkeypatch):
    class StubDataAPI:
        def daily_coverage(self, codes, start, end, fields):
            return {
                "coverage_version": "daily-coverage-v1",
                "market": "a-share",
                "source": "local:parquet",
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "expected_trading_days": 2,
                "requested_codes": codes,
                "items": [{"code": codes[0], "status": "complete"}],
                "complete": True,
                "coverage_hash": "a" * 64,
            }

    monkeypatch.setattr(market, "DataAPI", lambda: StubDataAPI())
    result = market.get_daily_coverage(
        codes="000001.SZ", start_date="2024-01-02", end_date="2024-01-03"
    )
    assert result["complete"] is True
    assert result["meta"] == {"research_only": True, "calendar": "TradingCalendar", "adjustment": "none"}


def test_daily_coverage_endpoint_rejects_invalid_codes():
    with pytest.raises(HTTPException) as raised:
        market.get_daily_coverage(codes="000001", start_date="2024-01-02", end_date="2024-01-03")
    assert raised.value.status_code == 422


def test_calendar_coverage_endpoint_returns_strict_provenance(monkeypatch):
    class StubCalendar:
        def __init__(self, start_year, end_year):
            assert (start_year, end_year) == (2024, 2024)

        def ensure_coverage(self, start, end):
            return {
                "calendar_version": "trading-calendar-v1",
                "source": "akshare:test",
                "content_hash": "b" * 64,
                "coverage_start": "2024-01-02",
                "coverage_end": "2024-12-31",
                "requested_start": start.isoformat(),
                "requested_end": end.isoformat(),
                "trading_days": [start, end],
                "verified": True,
                "complete": True,
            }

    monkeypatch.setattr(market, "TradingCalendar", StubCalendar)
    result = market.get_calendar_coverage(
        start_date="2024-01-02", end_date="2024-01-03", refresh=True
    )
    assert result["complete"] is True
    assert result["content_hash"] == "b" * 64
    assert result["meta"]["fail_closed"] is True


def test_calendar_coverage_endpoint_rejects_reversed_dates():
    with pytest.raises(HTTPException) as raised:
        market.get_calendar_coverage(
            start_date="2024-01-03", end_date="2024-01-02", refresh=False
        )
    assert raised.value.status_code == 422


def test_market_overview_uses_only_returned_quotes(monkeypatch):
    quotes = [
        quote("000001.SZ"),
        MarketQuote(**{**quote("600000.SH").to_dict(), "change_pct": -0.5}),
    ]
    monkeypatch.setattr(market, "live_market_provider", StubProvider(quotes))

    response = market.get_market_overview()

    assert response["data"]["quoted_count"] == 2
    assert response["data"]["advancers"] == 1
    assert response["data"]["decliners"] == 1


def test_market_overview_does_not_turn_missing_metrics_into_zero(monkeypatch):
    incomplete = MarketQuote(**{
        **quote("000001.SZ").to_dict(),
        "change_pct": None,
        "amount": None,
    })
    monkeypatch.setattr(market, "live_market_provider", StubProvider([incomplete]))

    response = market.get_market_overview()

    assert response["data"]["advancers"] is None
    assert response["data"]["decliners"] is None
    assert response["data"]["unchanged"] is None
    assert response["data"]["total_amount"] is None
    assert response["meta"]["status"] == "partial"
    assert response["meta"]["valid_change_count"] == 0
    assert response["meta"]["valid_amount_count"] == 0
    assert response["meta"]["freshness"] == "fresh"
    assert response["data"]["limit_up"] is None


def test_market_overview_marks_partial_when_metric_coverage_is_incomplete(monkeypatch):
    incomplete = MarketQuote(**{
        **quote("600000.SH").to_dict(),
        "change_pct": None,
        "amount": None,
    })
    provider = StubProvider([quote("000001.SZ"), incomplete])
    monkeypatch.setattr(market, "live_market_provider", provider)
    monkeypatch.setattr(market, "_overview_cache", None)

    response = market.get_market_overview()

    assert response["data"]["quoted_count"] == 2
    assert response["meta"]["valid_change_count"] == 1
    assert response["meta"]["valid_amount_count"] == 1
    assert response["meta"]["status"] == "partial"


def test_market_overview_preserves_delayed_freshness(monkeypatch):
    delayed = MarketQuote(**{
        **quote("000001.SZ").to_dict(),
        "freshness": "delayed",
    })
    monkeypatch.setattr(market, "live_market_provider", StubProvider([delayed]))
    monkeypatch.setattr(market, "_overview_cache", None)

    response = market.get_market_overview()

    assert response["meta"]["freshness"] == "delayed"


def test_market_overview_returns_503_for_empty_provider(monkeypatch):
    monkeypatch.setattr(market, "live_market_provider", StubProvider([]))

    with pytest.raises(HTTPException) as raised:
        market.get_market_overview()

    assert raised.value.status_code == 503
    assert raised.value.detail["code"] == "MARKET_BREADTH_EMPTY"


def test_default_market_overview_batches_security_master_and_caches(monkeypatch):
    requested = [f"{index:06d}.SZ" for index in range(1, 206)]
    available = [MarketQuote(**{**quote(code).to_dict(), "code": code}) for code in requested]

    class RecordingProvider(StubProvider):
        def __init__(self):
            super().__init__(available)
            self.calls = []

        def fetch_quotes(self, codes=None):
            self.calls.append(list(codes or []))
            return super().fetch_quotes(codes)

    class StubSecurityMaster:
        def snapshot(self, force=False):
            return ([{"code": code} for code in requested], {
                "source": "test:master", "freshness": "fresh",
            })

    provider = RecordingProvider()
    monkeypatch.setattr(market, "live_market_provider", provider)
    monkeypatch.setattr(market, "_DEFAULT_LIVE_MARKET_PROVIDER", provider)
    monkeypatch.setattr(market, "security_master_provider", StubSecurityMaster())
    monkeypatch.setattr(market, "_overview_cache", None)

    response = market.get_market_overview()
    assert sorted(len(batch) for batch in provider.calls) == [5, 100, 100]
    assert response["data"]["quoted_count"] == 205
    assert response["meta"]["requested_count"] == 205
    assert response["meta"]["coverage"] == "security_master_requested"
    assert response["meta"]["status"] == "ok"

    # A second read within the TTL is served from an isolated copy, avoiding
    # another full-universe provider fan-out.
    cached = market.get_market_overview()
    assert cached == response
    assert len(provider.calls) == 3


def test_market_overview_serves_stale_snapshot_while_refresh_lock_is_busy(monkeypatch):
    provider = StubProvider([quote("000001.SZ")])
    monkeypatch.setattr(market, "live_market_provider", provider)
    cached_response = {
        "data": {"quoted_count": 1, "advancers": 1, "decliners": 0,
                 "unchanged": 0, "total_amount": 10000.0,
                 "limit_up": None, "limit_down": None},
        "meta": {"sources": ["test"], "freshness": "fresh"},
    }
    monkeypatch.setattr(
        market,
        "_overview_cache",
        (time.monotonic() - market._OVERVIEW_CACHE_TTL_SECONDS - 1, provider, cached_response),
    )
    assert market._overview_cache_lock.acquire(blocking=False)
    try:
        response = market.get_market_overview()
    finally:
        market._overview_cache_lock.release()

    assert response["meta"]["cache_state"] == "stale"
    assert response["data"]["quoted_count"] == 1


def test_health_contract_and_read_only_local_coverage(monkeypatch, tmp_path):
    daily = tmp_path / "raw" / "daily" / "year=2025" / "quarter=1"
    daily.mkdir(parents=True)
    pd.DataFrame({"date": [date(2025, 1, 2)]}).to_parquet(daily / "000001.SZ.parquet")
    db_path = tmp_path / "meta.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE stock_info (code TEXT PRIMARY KEY)")
        conn.executemany("INSERT INTO stock_info VALUES (?)", [("000001.SZ",), ("600000.SH",)])

    monkeypatch.setattr(market, "live_market_provider", StubProvider())
    monkeypatch.setattr(market.settings, "data_dir", str(tmp_path))

    response = market.get_market_health()

    assert response["status"] == "degraded"
    assert response["latest_local_date"] == "2025-01-02"
    assert response["stock_count"] == 2
    assert len(response["providers"]) == 2


def test_health_degrades_when_security_master_is_unavailable(monkeypatch, tmp_path):
    class HealthyLiveProvider:
        def health(self):
            return [{"name": "tencent:qt", "status": "ok"}]

    class UnavailableSecurityMaster:
        def health(self):
            return {"name": "akshare:stock_master", "status": "unavailable"}

    monkeypatch.setattr(market, "live_market_provider", HealthyLiveProvider())
    monkeypatch.setattr(market, "security_master_provider", UnavailableSecurityMaster())
    monkeypatch.setattr(market.settings, "data_dir", str(tmp_path))

    response = market.get_market_health()

    assert response["status"] == "degraded"
    assert response["security_master"]["status"] == "unavailable"


def test_daily_falls_back_to_real_tencent_kline_when_local_window_is_empty(monkeypatch):
    class EmptyDataAPI:
        def daily(self, **kwargs):
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    class StubKlineProvider:
        def fetch_daily(self, code, start, end, adjust):
            return [{
                "code": code,
                "date": "2026-09-01",
                "open": 10.0,
                "close": 10.2,
                "high": 10.3,
                "low": 9.9,
                "volume": 123_400,
                "source": "tencent:kline",
                "adjust": "qfq",
            }]

    monkeypatch.setattr(market, "DataAPI", EmptyDataAPI)
    monkeypatch.setattr(market, "tencent_daily_provider", StubKlineProvider())

    response = market.get_daily(
        "000001.SZ", "2026-09-01", "2026-09-05", fields="open,high,low,close,volume"
    )

    assert response[0]["code"] == "000001.SZ"
    assert response[0]["source"] == "tencent:kline"


def test_universe_search_is_paginated_and_source_labelled(monkeypatch):
    class StubSecurityMaster:
        def snapshot(self, force=False):
            return [
                {"code": "000001.SZ", "name": "平安银行", "exchange": "SZ", "board": "主板", "listed_date": None, "delisted_date": None},
                {"code": "300750.SZ", "name": "宁德时代", "exchange": "SZ", "board": "创业板", "listed_date": None, "delisted_date": None},
            ], {"source": "test:master", "updated_at": "2026-09-05T00:00:00+00:00", "freshness": "fresh"}

    monkeypatch.setattr(market, "security_master_provider", StubSecurityMaster())

    response = market.list_universe(search="时代", board="创业板", page=1, page_size=1)

    assert response["data"][0]["code"] == "300750.SZ"
    assert response["meta"]["source"] == "test:master"
    assert response["meta"]["total_count"] == 1
    assert response["meta"]["returned_count"] == 1


def test_index_snapshot_archive_and_future_guard(monkeypatch):
    captured = {}

    class StubAPI:
        def archive_index_components(self, index_code, as_of, rows, source):
            captured.update(index_code=index_code, as_of=as_of, rows=rows, source=source)
            return len(rows)

    monkeypatch.setattr(market, "DataAPI", lambda: StubAPI())
    request = market.IndexSnapshotRequest(
        index_code="000300.SH",
        as_of=date(2024, 1, 31),
        rows=[market.IndexSnapshotRow(code="000001.SZ", name="平安银行", weight=1.2)],
        source="test:snapshot",
    )

    response = market.archive_index_snapshot(request)

    assert response["stored_count"] == 1
    assert captured["as_of"] == date(2024, 1, 31)
    with pytest.raises(HTTPException) as raised:
        market.archive_index_snapshot(market.IndexSnapshotRequest(
            index_code="000300.SH", as_of=date.today() + timedelta(days=1),
            rows=[market.IndexSnapshotRow(code="000001.SZ", name="平安银行")],
        ))
    assert raised.value.status_code == 422
    with pytest.raises(ValueError, match="conflicts"):
        market.IndexSnapshotRow(code="600519.BJ", name="贵州茅台")


def test_index_snapshot_import_dry_run_never_writes(monkeypatch):
    calls = []

    class StubAPI:
        def validate_index_components_batch(self, snapshots):
            calls.append(("validate", snapshots))
            return [{"index_code": "999999.SH", "as_of": "2024-01-31",
                     "source": "test", "constituent_count": 1}]

        def archive_index_components_batch(self, snapshots):
            calls.append(("write", snapshots))
            return 1

    monkeypatch.setattr(market, "DataAPI", lambda: StubAPI())
    request = market.IndexSnapshotImportRequest(
        dry_run=True,
        snapshots=[market.IndexSnapshotRequest(
            index_code="999999.SH", as_of=date(2024, 1, 31),
            rows=[market.IndexSnapshotRow(code="000001.SZ", name="平安银行")],
            source="test",
        )],
    )

    response = market.import_index_snapshots(request)

    assert response["status"] == "validated"
    assert response["total_constituents"] == 1
    assert [kind for kind, _ in calls] == ["validate"]


def test_index_snapshot_import_rejects_duplicate_period_before_validation(monkeypatch):
    class ShouldNotBeCalled:
        def validate_index_components_batch(self, _snapshots):
            raise AssertionError("duplicate periods must fail before storage validation")

    monkeypatch.setattr(market, "DataAPI", lambda: ShouldNotBeCalled())
    snapshot = market.IndexSnapshotRequest(
        index_code="999999.SH", as_of=date(2024, 1, 31),
        rows=[market.IndexSnapshotRow(code="000001.SZ", name="平安银行")],
    )
    with pytest.raises(HTTPException) as raised:
        market.import_index_snapshots(market.IndexSnapshotImportRequest(
            snapshots=[snapshot, snapshot],
        ))
    assert raised.value.status_code == 422
    assert "duplicate" in str(raised.value.detail).lower()


def test_index_snapshot_coverage_reports_available_periods(monkeypatch):
    class StubAPI:
        def index_snapshot_coverage(self, index_code):
            assert index_code == "999999.SH"
            return [
                {"as_of": "2024-01-31", "constituent_count": 1},
                {"as_of": "2024-02-29", "constituent_count": 2},
            ]

    monkeypatch.setattr(market, "DataAPI", lambda: StubAPI())
    response = market.get_index_snapshot_coverage("999999.SH")
    assert response["meta"]["snapshot_count"] == 2
    assert response["meta"]["first_as_of"] == "2024-01-31"
    assert response["meta"]["latest_as_of"] == "2024-02-29"


def test_indexes_return_major_index_cards(monkeypatch):
    class StubIndexProvider:
        def fetch_quotes(self, codes):
            return [quote(code) for code in codes]

    monkeypatch.setattr(market, "index_market_provider", StubIndexProvider())

    response = market.list_indexes()

    assert response["meta"]["status"] == "ok"
    assert len(response["data"]) == 6
    assert response["data"][0]["name"] == "上证指数"
    assert response["data"][0]["asset_type"] == "index"
    assert response["data"][0]["currency"] == "CNY"


def test_universe_returns_503_when_source_and_cache_are_unavailable(monkeypatch):
    class UnavailableSecurityMaster:
        def snapshot(self, force=False):
            raise market.SecurityMasterUnavailableError([
                {"source": "akshare:stock_master", "error": "TimeoutError: timeout"}
            ])

    monkeypatch.setattr(market, "security_master_provider", UnavailableSecurityMaster())

    with pytest.raises(HTTPException) as raised:
        market.list_universe()

    assert raised.value.status_code == 503
    assert raised.value.detail["code"] == "SECURITY_MASTER_UNAVAILABLE"


def test_daily_falls_back_when_local_window_is_only_partially_covered(monkeypatch):
    class PartialDataAPI:
        def daily(self, **kwargs):
            return pd.DataFrame({
                "open": [10.0], "high": [10.3], "low": [9.9],
                "close": [10.2], "volume": [123_400],
            }, index=pd.MultiIndex.from_tuples(
                [("000001.SZ", date(2026, 1, 2))], names=["code", "date"]
            ))

    calls = []

    class StubKlineProvider:
        def fetch_daily(self, code, start, end, adjust):
            calls.append((code, start, end, adjust))
            return [{"code": code, "date": "2026-01-02", "close": 10.2, "source": "tencent:kline"}]

    monkeypatch.setattr(market, "DataAPI", PartialDataAPI)
    monkeypatch.setattr(market, "tencent_daily_provider", StubKlineProvider())

    response = market.get_daily("000001.SZ", "2026-01-01", "2026-09-05")

    assert calls == [("000001.SZ", date(2026, 1, 1), date(2026, 9, 5), "event_driven")]
    assert response[0]["source"] == "tencent:kline"


def test_daily_does_not_trust_a_truncated_local_calendar(monkeypatch):
    class TruncatedDataAPI:
        def daily(self, **kwargs):
            return pd.DataFrame({
                "open": [10.0], "high": [10.3], "low": [9.9],
                "close": [10.2], "volume": [123_400],
            }, index=pd.MultiIndex.from_tuples(
                [("000001.SZ", date(2024, 12, 1))], names=["code", "date"]
            ))

        def get_trading_dates(self, start, end):
            # The bundled calendar ends in 2024 even though the request ends
            # in 2026; this must not make the local row look complete.
            return [date(2024, 12, 1), date(2024, 12, 31)]

    class StubKlineProvider:
        def fetch_daily(self, code, start, end, adjust):
            return [{
                "code": code, "date": "2026-01-02", "open": 11.0,
                "high": 11.3, "low": 10.9, "close": 11.2,
                "volume": 12_000, "source": "tencent:kline", "adjust": "qfq",
            }]

    monkeypatch.setattr(market, "DataAPI", TruncatedDataAPI)
    monkeypatch.setattr(market, "tencent_daily_provider", StubKlineProvider())
    response = market.get_daily("000001.SZ", "2024-01-01", "2026-09-05")
    assert response[0]["source"] == "tencent:kline"


def test_daily_keeps_local_data_when_end_date_is_weekend(monkeypatch):
    class LocalDataAPI:
        def daily(self, **kwargs):
            return pd.DataFrame({
                "open": [10.0, 10.1, 10.2, 10.3],
                "high": [10.2, 10.3, 10.4, 10.5],
                "low": [9.9, 10.0, 10.1, 10.2],
                "close": [10.1, 10.2, 10.3, 10.4],
            }, index=pd.MultiIndex.from_tuples([
                ("000001.SZ", date(2026, 9, 1)),
                ("000001.SZ", date(2026, 9, 2)),
                ("000001.SZ", date(2026, 9, 3)),
                ("000001.SZ", date(2026, 9, 4)),
            ], names=["code", "date"]))

        def get_trading_dates(self, start, end):
            return [date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3), date(2026, 9, 4)]

    class FailingKlineProvider:
        def fetch_daily(self, *args, **kwargs):
            raise AssertionError("remote fallback should not be called")

    monkeypatch.setattr(market, "DataAPI", LocalDataAPI)
    monkeypatch.setattr(market, "tencent_daily_provider", FailingKlineProvider())

    response = market.get_daily("000001.SZ", "2026-09-01", "2026-09-05", fields="close")

    assert len(response) == 4
    assert response[-1]["date"] == "2026-09-04"


def test_daily_rejects_invalid_or_reversed_date_ranges():
    with pytest.raises(HTTPException) as invalid:
        market.get_daily("000001.SZ", "not-a-date", "2026-09-05")
    assert invalid.value.status_code == 422
    assert invalid.value.detail["code"] == "INVALID_DATE_RANGE"

    with pytest.raises(HTTPException) as reversed_range:
        market.get_daily("000001.SZ", "2026-09-05", "2026-09-01")
    assert reversed_range.value.status_code == 422
    assert reversed_range.value.detail["code"] == "INVALID_DATE_RANGE"


def test_daily_rejects_remote_range_beyond_provider_limit(monkeypatch):
    class EmptyDataAPI:
        def daily(self, **kwargs):
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    monkeypatch.setattr(market, "DataAPI", EmptyDataAPI)

    with pytest.raises(HTTPException) as raised:
        market.get_daily("000001.SZ", "2020-01-01", "2026-09-05")

    assert raised.value.status_code == 422
    assert raised.value.detail["code"] == "HISTORICAL_RANGE_TOO_LARGE"


def test_daily_returns_empty_for_weekend_only_window(monkeypatch):
    class EmptyDataAPI:
        def daily(self, **kwargs):
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        def get_trading_dates(self, start, end):
            return []

    class FailingKlineProvider:
        def fetch_daily(self, *args, **kwargs):
            raise AssertionError("weekend-only window should not call remote source")

    monkeypatch.setattr(market, "DataAPI", EmptyDataAPI)
    monkeypatch.setattr(market, "tencent_daily_provider", FailingKlineProvider())

    assert market.get_daily("000001.SZ", "2026-09-05", "2026-09-06") == []


def test_candles_aggregate_weekly_and_report_source_adjustment(monkeypatch):
    daily = [
        {"code": "000001.SZ", "date": "2026-01-02", "open": 10, "high": 12, "low": 9, "close": 11, "volume": 2, "source": "tencent:kline", "adjust": "qfq"},
        {"code": "000001.SZ", "date": "2026-01-05", "open": 11, "high": 13, "low": 10, "close": 12, "volume": 3, "source": "tencent:kline", "adjust": "qfq"},
        {"code": "000001.SZ", "date": "2026-01-09", "open": 12, "high": 14, "low": 11, "close": 13, "volume": 4, "source": "tencent:kline", "adjust": "qfq"},
    ]
    monkeypatch.setattr(market, "get_daily", lambda *args, **kwargs: daily)

    response = market.get_candles(
        "000001.SZ", "2026-01-01", "2026-01-31", interval="1w",
        adjust="event_driven", indicators="ma",
    )

    assert response["meta"]["interval"] == "1w"
    assert response["meta"]["adjust_requested"] == "event_driven"
    assert response["meta"]["adjust_applied"] == "qfq"
    assert response["meta"]["warning_codes"] == ["ADJUSTMENT_FALLBACK"]
    assert response["meta"]["source"] == "tencent:kline"
    assert response["data"][0]["date"] == "2026-01-02"
    assert response["data"][1]["date"] == "2026-01-09"
    assert response["data"][1]["volume"] == 7.0


def test_candles_reject_invalid_indicator_and_range(monkeypatch):
    monkeypatch.setattr(market, "get_daily", lambda *args, **kwargs: [])
    with pytest.raises(HTTPException) as indicator:
        market.get_candles("000001.SZ", "2026-01-01", "2026-01-31", indicators="not-real")
    assert indicator.value.status_code == 422
    assert indicator.value.detail["code"] == "INVALID_CANDLE_REQUEST"

    with pytest.raises(HTTPException) as interval:
        market.get_candles("000001.SZ", "2026-01-01", "2026-01-31", interval="5m")
    assert interval.value.status_code == 422
    assert interval.value.detail["code"] == "INVALID_CANDLE_REQUEST"

    with pytest.raises(HTTPException) as too_large:
        market.get_candles("000001.SZ", "2020-01-01", "2026-01-31")
    assert too_large.value.status_code == 422
    assert too_large.value.detail["code"] == "HISTORICAL_RANGE_TOO_LARGE"


def test_candles_empty_result_is_explicit(monkeypatch):
    monkeypatch.setattr(market, "get_daily", lambda *args, **kwargs: [])
    response = market.get_candles("000001.SZ", "2026-01-03", "2026-01-04", interval="1mo")
    assert response["data"] == []
    assert response["meta"]["status"] == "empty"
    assert response["meta"]["source"] == "local:parquet"


def test_candles_are_cached_for_identical_queries(monkeypatch):
    rows = [{
        "code": "600000.SH", "date": "2026-01-02", "open": 10,
        "high": 11, "low": 9, "close": 10.5, "volume": 100,
    }]
    calls = []

    def fetch(*args, **kwargs):
        calls.append(1)
        return rows

    market._candle_cache.clear()
    monkeypatch.setattr(market, "get_daily", fetch)
    first = market.get_candles("600000.SH", "2026-01-01", "2026-01-31")
    second = market.get_candles("600000.SH", "2026-01-01", "2026-01-31")

    assert len(calls) == 1
    assert second["meta"]["cache"] == "memory"
    assert first["data"] == second["data"]


def test_candles_map_storage_or_provider_errors_to_structured_503(monkeypatch):
    market._candle_cache.clear()
    monkeypatch.setattr(market, "get_daily", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("broken source")))
    with pytest.raises(HTTPException) as raised:
        market.get_candles("600036.SH", "2026-01-01", "2026-01-31")
    assert raised.value.status_code == 503
    assert raised.value.detail["code"] == "CANDLE_DATA_UNAVAILABLE"
