from datetime import date, datetime, timezone
import json

import pandas as pd
import pytest

from quant_engine.data.live import (
    AKShareLiveMarketDataProvider,
    FallbackLiveMarketDataProvider,
    LiveMarketDataProvider,
    MarketDataUnavailableError,
    TencentDailyKlineProvider,
    TencentLiveMarketDataProvider,
)


def eastmoney_frame(*codes: str) -> pd.DataFrame:
    return pd.DataFrame([
        {
            "代码": code,
            "名称": f"股票{code}",
            "最新价": 10.5,
            "涨跌幅": 1.25,
            "成交量": 1234,
            "成交额": 5678,
        }
        for code in codes
    ])


def sina_frame(*codes: str) -> pd.DataFrame:
    return pd.DataFrame([
        {
            "代码": code,
            "名称": f"股票{code}",
            "最新价": 10.6,
            "涨跌幅": 1.5,
            "成交量": 2234,
            "成交额": 6678,
            "时间戳": datetime.now(timezone.utc).isoformat(),
        }
        for code in codes
    ])


def test_live_provider_is_abstract():
    with pytest.raises(TypeError):
        LiveMarketDataProvider()


def test_eastmoney_quote_does_not_invent_source_timestamp():
    provider = AKShareLiveMarketDataProvider(
        eastmoney_fetcher=lambda: eastmoney_frame("000001"),
        sina_fetcher=lambda: sina_frame("000001"),
    )

    quote = provider.fetch_quotes(["000001.SZ"])[0]

    assert quote.code == "000001.SZ"
    assert quote.source == "akshare:eastmoney"
    assert quote.as_of is None
    assert quote.freshness == "unknown"
    assert quote.is_fallback is False
    assert quote.received_at


def test_sina_is_used_after_eastmoney_failure():
    def fail():
        raise ConnectionError("eastmoney unavailable")

    provider = AKShareLiveMarketDataProvider(
        eastmoney_fetcher=fail,
        sina_fetcher=lambda: sina_frame("600000"),
    )

    quote = provider.fetch_quotes(["600000.SH"])[0]
    health = {item["name"]: item for item in provider.health()}

    assert quote.source == "akshare:sina"
    assert quote.is_fallback is True
    assert quote.as_of is not None
    assert quote.freshness == "fresh"
    assert health["akshare:eastmoney"]["status"] == "unavailable"
    assert health["akshare:sina"]["status"] == "ok"


def test_sina_fills_codes_missing_from_eastmoney():
    provider = AKShareLiveMarketDataProvider(
        eastmoney_fetcher=lambda: eastmoney_frame("000001"),
        sina_fetcher=lambda: sina_frame("600000"),
    )

    quotes = provider.fetch_quotes(["000001.SZ", "600000.SH"])
    by_code = {quote.code: quote for quote in quotes}

    assert by_code["000001.SZ"].is_fallback is False
    assert by_code["600000.SH"].is_fallback is True


def test_all_failures_are_explicit_and_contain_no_quotes():
    def fail():
        raise TimeoutError("timed out")

    provider = AKShareLiveMarketDataProvider(fail, fail)

    with pytest.raises(MarketDataUnavailableError) as raised:
        provider.fetch_quotes(["000001.SZ"])

    assert len(raised.value.attempts) == 2
    assert all("timed out" in attempt["error"] for attempt in raised.value.attempts)


def _tencent_payload(code: str = "000001", exchange: str = "sz") -> bytes:
    fields = ["51", "平安银行", code, "11.89", "11.88", "11.86", "814373"]
    fields.extend([""] * (30 - len(fields)))
    fields.extend(["20260905161500", "0.01", "0.08", "12.00", "11.85"])
    fields.extend([""] * (37 - len(fields)))
    fields.append("1234")
    payload = "~".join(fields)
    return f'v_{exchange}{code}="{payload}";'.encode("gbk")


def test_tencent_provider_parses_real_public_snapshot_shape():
    provider = TencentLiveMarketDataProvider(
        fetcher=lambda url, timeout: _tencent_payload(),
    )

    quote = provider.fetch_quotes(["000001.SZ"])[0]

    assert quote.code == "000001.SZ"
    assert quote.name == "平安银行"
    assert quote.price == pytest.approx(11.89)
    assert quote.change_pct == pytest.approx(0.08)
    assert quote.volume == pytest.approx(81_437_300)
    assert quote.amount == pytest.approx(12_340_000)
    assert quote.source == "tencent:qt"
    assert quote.as_of is not None
    assert provider.health()[0]["status"] == "ok"


def test_fallback_provider_fills_quotes_and_marks_provenance():
    def fail():
        raise ConnectionError("primary unavailable")

    primary = AKShareLiveMarketDataProvider(fail, fail)
    tencent = TencentLiveMarketDataProvider(
        fetcher=lambda url, timeout: _tencent_payload(),
    )
    provider = FallbackLiveMarketDataProvider([primary, tencent])

    quote = provider.fetch_quotes(["000001.SZ"])[0]

    assert quote.source == "tencent:qt"
    assert quote.is_fallback is True
    assert provider.health()[0]["status"] == "unavailable"
    assert provider.health()[-1]["status"] == "ok"


def test_tencent_daily_provider_parses_qfq_kline_shape():
    payload = {
        "code": 0,
        "data": {
            "sz000001": {
                "qfqday": [
                    ["2026-09-01", "10.0", "10.2", "10.3", "9.9", "1234"],
                    ["2026-09-02", "10.2", "10.1", "10.4", "10.0", "2345"],
                ]
            }
        },
    }
    provider = TencentDailyKlineProvider(
        fetcher=lambda url, timeout: json.dumps(payload).encode("utf-8"),
    )

    rows = provider.fetch_daily("000001.SZ", date(2026, 9, 1), date(2026, 9, 2))

    assert [row["date"] for row in rows] == ["2026-09-01", "2026-09-02"]
    assert rows[0]["open"] == pytest.approx(10.0)
    assert rows[0]["close"] == pytest.approx(10.2)
    assert rows[0]["volume"] == pytest.approx(123_400)
    assert rows[0]["source"] == "tencent:kline"


def test_tencent_daily_provider_uses_raw_day_for_index_levels():
    payload = {
        "code": 0,
        "data": {
            "sh000001": {
                "day": [
                    ["2026-09-01", "3900.0", "3910.0", "3920.0", "3890.0", "1234"],
                    ["2026-09-02", "3910.0", "3920.0", "3930.0", "3900.0", "2345"],
                ]
            }
        },
    }
    urls: list[str] = []
    provider = TencentDailyKlineProvider(
        fetcher=lambda url, timeout: (urls.append(url) or json.dumps(payload).encode("utf-8")),
    )

    rows = provider.fetch_daily("000001.SH", date(2026, 9, 1), date(2026, 9, 2))

    assert len(rows) == 2
    assert rows[0]["code"] == "000001.SH"
    assert rows[0]["adjust"] == "none"
    assert ",day,,," in urls[0]


def test_tencent_daily_provider_rejects_unbounded_history_request():
    provider = TencentDailyKlineProvider(fetcher=lambda url, timeout: b"{}")

    with pytest.raises(MarketDataUnavailableError) as raised:
        provider.fetch_daily("000001.SZ", date(2020, 1, 1), date(2026, 9, 5))

    assert raised.value.attempts[0]["source"] == "tencent:kline"
    assert "1000_calendar_days" in raised.value.attempts[0]["error"]
