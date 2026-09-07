from datetime import date, datetime, timezone
import json

import pytest

from quant_engine.data.global_markets import EastmoneyFundDataProvider, GoldMarketDataProvider, YahooUSMarketDataProvider
from quant_engine.data.live import MarketDataUnavailableError
from server.api import market


def _yahoo_payload():
    stamps = [
        int(datetime(2026, 9, 3, 20, 0, tzinfo=timezone.utc).timestamp()),
        int(datetime(2026, 9, 4, 20, 0, tzinfo=timezone.utc).timestamp()),
    ]
    return {"chart": {"result": [{
        "meta": {
            "symbol": "AAPL", "longName": "Apple Inc.", "currency": "USD",
            "regularMarketPrice": 101.0, "previousClose": 100.0,
            "regularMarketVolume": 123456, "regularMarketTime": stamps[-1],
        },
        "timestamp": stamps,
        "indicators": {"quote": [{
            "open": [99.0, 100.0], "high": [101.0, 102.0],
            "low": [98.0, 99.0], "close": [100.0, 101.0],
            "volume": [1000, 2000],
        }]},
    }]}}


def _fund_payload():
    return {"Data": {"LSJZList": [
        {"FSRQ": "2026-09-03", "DWJZ": "1.1000", "JZZZL": "0.91"},
        {"FSRQ": "2026-09-04", "DWJZ": "1.1200", "JZZZL": "1.82"},
    ]}}


def test_yahoo_provider_returns_quote_and_daily_rows():
    provider = YahooUSMarketDataProvider(fetcher=lambda url, timeout: _yahoo_payload())
    quote = provider.fetch_quotes(["aapl"])[0]
    assert quote.code == "AAPL"
    assert quote.price == pytest.approx(101.0)
    assert quote.change_pct == pytest.approx(1.0)
    assert quote.market == "US"
    assert quote.currency == "USD"
    rows = provider.fetch_daily("AAPL", date(2026, 9, 3), date(2026, 9, 4))
    assert [row["date"] for row in rows] == ["2026-09-03", "2026-09-04"]
    assert rows[0]["source"] == "yahoo:chart"


def test_yahoo_provider_accepts_broad_index_symbols_as_non_tradable_quotes():
    payload = _yahoo_payload()
    payload["chart"]["result"][0]["meta"].update({
        "symbol": "^NDX",
        "longName": "NASDAQ 100",
    })
    provider = YahooUSMarketDataProvider(fetcher=lambda url, timeout: payload)
    quote = provider.fetch_quotes(["^ndx"])[0]
    assert quote.code == "^NDX"
    assert quote.name == "纳斯达克100"
    assert quote.asset_type == "index"


def test_yahoo_provider_rejects_invalid_symbol_and_reports_total_failure():
    provider = YahooUSMarketDataProvider(fetcher=lambda url, timeout: {"chart": {"result": []}})
    with pytest.raises(ValueError):
        provider.fetch_quotes(["AAPL/EVIL"])
    with pytest.raises(MarketDataUnavailableError) as raised:
        provider.fetch_quotes(["AAPL"])
    assert raised.value.attempts[0]["source"] == "yahoo:chart"


def test_fund_provider_returns_nav_quote_and_flat_daily_bars():
    provider = EastmoneyFundDataProvider(fetcher=lambda url, timeout: _fund_payload())
    quote = provider.fetch_quotes(["fund:110022"])[0]
    assert quote.code == "110022"
    assert quote.asset_type == "fund"
    assert quote.currency == "CNY"
    assert quote.price == pytest.approx(1.12)
    assert quote.change_pct == pytest.approx(1.82)
    rows = provider.fetch_daily("110022", date(2026, 9, 3), date(2026, 9, 4))
    assert rows[1]["open"] == rows[1]["close"] == pytest.approx(1.12)
    assert rows[1]["volume"] is None


def test_fund_provider_uses_public_chart_script_when_json_endpoint_is_empty():
    raw = 'var fS_name = "Demo"; Data_netWorthTrend = [{"x":1788379200000,"y":1.1,"equityReturn":1.0},{"x":1788465600000,"y":1.12,"equityReturn":1.82}];'
    provider = EastmoneyFundDataProvider(
        fetcher=lambda url, timeout: {"Data": {"LSJZList": None}},
        text_fetcher=lambda url, timeout: raw,
    )
    quote = provider.fetch_quotes(["110022"])[0]
    assert quote.price == pytest.approx(1.12)
    assert quote.change_pct == pytest.approx(1.82)


def test_gold_provider_falls_back_to_explicit_comex_reference_for_stale_domestic_history():
    yahoo = YahooUSMarketDataProvider(fetcher=lambda url, timeout: _yahoo_payload())
    provider = GoldMarketDataProvider(
        yahoo_provider=yahoo,
        text_fetcher=lambda url, timeout: "[]",
    )
    rows = provider.fetch_daily("AU0", date(2026, 9, 3), date(2026, 9, 4))
    assert len(rows) == 2
    assert {row["code"] for row in rows} == {"AU0"}
    assert {row["reference_symbol"] for row in rows} == {"GC=F"}
    assert all(row["reference_only"] is True for row in rows)


def test_gold_provider_uses_comex_reference_when_xau_spot_history_is_unavailable():
    def fetcher(url, timeout):
        if "/XAU?" in url:
            return {"chart": {"result": []}}
        return _yahoo_payload()
    yahoo = YahooUSMarketDataProvider(fetcher=fetcher)
    provider = GoldMarketDataProvider(yahoo_provider=yahoo)
    rows = provider.fetch_daily("XAU", date(2026, 9, 3), date(2026, 9, 4))
    assert {row["code"] for row in rows} == {"XAU"}
    assert {row["reference_symbol"] for row in rows} == {"GC=F"}


def test_cross_market_routes_keep_market_metadata(monkeypatch):
    us = YahooUSMarketDataProvider(fetcher=lambda url, timeout: _yahoo_payload())
    fund = EastmoneyFundDataProvider(fetcher=lambda url, timeout: _fund_payload())
    gold = GoldMarketDataProvider(
        yahoo_provider=YahooUSMarketDataProvider(fetcher=lambda url, timeout: _yahoo_payload()),
        text_fetcher=lambda url, timeout: "[]",
    )
    monkeypatch.setitem(market._cross_market_providers, "us-equity", us)
    monkeypatch.setitem(market._cross_market_providers, "cn-fund", fund)
    monkeypatch.setitem(market._cross_market_providers, "gold", gold)

    markets = market.list_supported_markets()
    assert [item["id"] for item in markets["data"]] == ["a-share", "cn-fund", "us-equity", "gold"]
    quotes = market.get_cross_market_quotes("us-equity", "AAPL")
    assert quotes["data"][0]["currency"] == "USD"
    candles = market.get_cross_market_candles(
        "cn-fund", "110022", "2026-09-03", "2026-09-04", "1d", None
    )
    assert candles["meta"]["market"] == "cn-fund"
    assert candles["meta"]["note"].startswith("基金净值")
    gold_candles = market.get_cross_market_candles(
        "gold", "AU0", "2026-09-03", "2026-09-04", "1d", None
    )
    assert "GC=F" in gold_candles["meta"]["note"]


def test_cross_market_route_rejects_unknown_market():
    with pytest.raises(Exception) as raised:
        market.get_cross_market_quotes("mars", "AAPL")
    assert getattr(raised.value, "status_code", None) == 422
