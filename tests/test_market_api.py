from datetime import date, datetime, timezone
import sqlite3

from fastapi import HTTPException
import pandas as pd

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
