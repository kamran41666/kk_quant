"""Refresh the local A-share trading calendar from AKShare.

The cache is written atomically and includes the provider in every row.  A
fallback business-day calendar is never persisted by this script, because it
cannot be used for a research backtest.
"""
from __future__ import annotations

import json
from datetime import date

from quant_engine.data.calendar import TradingCalendar


def main() -> int:
    calendar = TradingCalendar(start_year=1990, end_year=date.today().year + 1)
    fetched = calendar._fetch_from_akshare()
    source = str(fetched["source"].dropna().iloc[0]) if not fetched.empty else ""
    if not source.startswith("akshare:"):
        raise SystemExit("AKShare calendar refresh failed; no cache was changed")
    calendar._write_cache(calendar._data_path(), fetched)
    refreshed = TradingCalendar(start_year=1990, end_year=date.today().year + 1)
    print(json.dumps({
        "source": refreshed.source,
        "content_hash": refreshed.content_hash,
        "coverage_start": refreshed.coverage_report(date(1990, 1, 1), date.today())["coverage_start"],
        "coverage_end": refreshed.coverage_report(date(1990, 1, 1), date.today())["coverage_end"],
        "trading_days": len(refreshed.all_dates),
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
