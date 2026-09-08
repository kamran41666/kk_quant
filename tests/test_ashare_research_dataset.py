import pandas as pd
import pytest

from scripts.build_ashare_research_dataset import merge_supplemental_actions, normalize_actions, normalize_daily, reference_return_prices, security_quality


def raw_daily():
    return pd.DataFrame([{
        "date": day, "code": "sh.600000", "open": "10", "high": "11", "low": "9", "close": "10",
        "preclose": "10", "volume": "100", "amount": "1000", "turn": "1", "tradestatus": "1", "isST": "0",
    } for day in ["2024-01-02", "2024-01-03"]])


def test_adjusted_signal_never_uses_future_factor():
    factors = pd.DataFrame({"dividOperateDate": ["2024-01-03", "2024-01-04"], "backAdjustFactor": ["2", "8"]})
    normalized = normalize_daily(raw_daily(), factors, "sh.600000")
    assert normalized.close.tolist() == [10, 10]
    assert normalized.adjusted_close.tolist() == [10, 20]
    assert normalized.volume.tolist() == [100, 100]
    assert normalized.turnover_rate.tolist() == [.01, .01]


def test_reference_signal_ignores_misdated_vendor_factor_and_is_prefix_stable():
    raw = raw_daily()
    raw.loc[1, ["open", "high", "low", "close", "preclose"]] = ["5", "5", "5", "5", "5"]
    wrong_factor = pd.DataFrame({"dividOperateDate": ["2024-01-04"], "backAdjustFactor": ["2"]})
    normalized = normalize_daily(raw, wrong_factor, "sh.600000", "reference_chain")
    assert normalized.adjusted_close.tolist() == [10, 10]
    assert normalized.vendor_adjusted_close.tolist() == [10, 5]
    assert reference_return_prices(raw.iloc[:1]).iloc[0] == normalized.adjusted_close.iloc[0]
    raw.loc[1, "preclose"] = "0"
    assert pd.isna(reference_return_prices(raw).iloc[1])


def test_stock_bonus_and_capitalization_are_added_without_changing_event_dates():
    frame = pd.DataFrame([{"dividOperateDate": "2024-01-03", "dividRegistDate": "2024-01-02",
                           "dividPayDate": "2024-01-05", "dividStockMarketDate": "2024-01-08",
                           "dividCashPsBeforeTax": "0.5", "dividStocksPs": "0.1", "dividReserveToStockPs": "0.2"}])
    row = normalize_actions(frame, "sh.600000", "2024-12-31").iloc[0]
    assert row.cash_ps == .5
    assert row.bonus_ratio == pytest.approx(.3)
    assert row.record_date == "2024-01-02"
    assert row.pay_date == "2024-01-05"
    assert row.stock_date == "2024-01-08"


def test_duplicate_partial_dividend_does_not_turn_missing_cash_into_zero():
    row = {"dividOperateDate": "2021-07-09", "dividRegistDate": "2021-07-08",
           "dividPayDate": "2021-07-09", "dividStockMarketDate": "",
           "dividCashPsBeforeTax": "0.421", "dividStocksPs": "0", "dividReserveToStockPs": ""}
    result = normalize_actions(pd.DataFrame([row, {**row, "dividCashPsBeforeTax": ""}]), "sh.600461", "2026-08-31")
    assert len(result) == 1
    assert result.iloc[0].cash_ps == .421
    with pytest.raises(ValueError, match="conflicting"):
        normalize_actions(pd.DataFrame([row, {**row, "dividCashPsBeforeTax": "0.5"}]), "sh.600461", "2026-08-31")


def test_same_day_independent_payments_need_explicit_sourced_resolution():
    rows = pd.DataFrame([{"dividOperateDate": "2013-06-14", "dividCashPsBeforeTax": cash,
                          "dividStocksPs": "0", "dividReserveToStockPs": "",
                          "dividRegistDate": "2013-06-13", "dividPayDate": "2013-06-14",
                          "dividStockMarketDate": ""} for cash in ["0.021", "0.041"]])
    with pytest.raises(ValueError, match="conflicting"):
        normalize_actions(rows, "sz.000738", "2026-08-31")
    evidence = [{"code": "000738.SZ", "ex_date": "2013-06-14", "cash_parts": [.021, .041],
                 "cash_ps": .062, "source_url": "https://example.test/issuer-announcement"}]
    result = normalize_actions(rows, "sz.000738", "2026-08-31", evidence)
    assert result.iloc[0].cash_ps == .062


def test_coverage_classifies_lifecycle_without_dropping_delisted_security():
    daily = normalize_daily(raw_daily(), pd.DataFrame(), "sh.600000")
    metadata = {"code": "600000.SH", "ipo_date": "2024-01-02", "out_date": "2024-01-04"}
    report = security_quality(daily, pd.DataFrame(), metadata, pd.date_range("2024-01-01", "2024-01-05"))
    assert report["missing_count"] == 0
    assert report["expected_sessions"] == 2
    assert report["out_date"] == "2024-01-04"


def test_unknown_reference_adjustments_are_reported_not_cash_compensated():
    daily = normalize_daily(raw_daily(), pd.DataFrame(), "sh.600000")
    daily.loc[1, "preclose"] = 8
    metadata = {"code": "600000.SH", "ipo_date": "2000-01-01", "out_date": None}
    report = security_quality(daily, pd.DataFrame(), metadata, pd.DatetimeIndex(daily.date))
    assert report["unexplained_reference_adjustments"] == [{
        "date": "2024-01-03", "previous_close": 10., "reference": 8.,
        "expected_reference": 10., "action_present": False,
    }]


def test_unit_audit_flags_volume_in_lots_mislabeled_as_shares():
    daily = normalize_daily(raw_daily(), pd.DataFrame(), "sh.600000")
    daily["volume"] /= 100
    metadata = {"code": "600000.SH", "ipo_date": "2000-01-01", "out_date": None}
    report = security_quality(daily, pd.DataFrame(), metadata, pd.DatetimeIndex(daily.date))
    assert report["price_volume_amount_outliers"] == 2


def test_sourced_supplement_fills_dates_but_never_replaces_economic_facts():
    columns = ["code", "record_date", "ex_date", "pay_date", "stock_date", "cash_ps", "bonus_ratio"]
    original = pd.DataFrame([["600167.SH", "2024-12-26", "2024-12-27", None, None, .12, 0]], columns=columns)
    source = {**original.iloc[0].to_dict(), "pay_date": "2024-12-27", "source_url": "https://example.test/issuer", "source_sha256": "a" * 64}
    result = merge_supplemental_actions(original, [source], "sh.600167")
    assert result.iloc[0].pay_date == "2024-12-27"
    assert original.iloc[0].pay_date is None
    with pytest.raises(ValueError, match="conflicts"):
        merge_supplemental_actions(original, [{**source, "cash_ps": .2}], "sh.600167")
