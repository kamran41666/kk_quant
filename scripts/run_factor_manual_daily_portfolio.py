"""Run the archived-input manual daily portfolio simulator.

The CLI intentionally accepts JSON signals and a parquet bar file; it does
not discover data providers or connect to a broker.  Projects can import
``run_manual_daily_portfolio`` for richer orchestration.
"""
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

import pandas as pd

from quant_engine.factor.manual_daily_bundle import ManualDailyFactorBundleV2
from quant_engine.backtest.manual_daily_portfolio import run_manual_daily_portfolio


class FileCalendar:
    def __init__(self, days: list[date], content_hash: str):
        self.days, self.content_hash = days, content_hash

    def ensure_coverage(self, start, end):
        return {"complete": start >= self.days[0] and end <= self.days[-1], "content_hash": self.content_hash, "source": "archived_cli_input"}

    def get_trading_days(self, start, end):
        return [day for day in self.days if start <= day <= end]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bars", type=Path, required=True)
    parser.add_argument("--signals", type=Path, required=True)
    parser.add_argument("--calendar", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    bundle_payload = json.loads(args.bundle.read_text(encoding="utf-8"))
    # bundle JSON is the public identity; construction intentionally requires
    # the minimal explicit evidence fields instead of trusting bundle_hash.
    bundle = ManualDailyFactorBundleV2(**bundle_payload)
    calendar_payload = json.loads(args.calendar.read_text(encoding="utf-8"))
    calendar = FileCalendar([date.fromisoformat(value) for value in calendar_payload["trading_days"]], calendar_payload["content_hash"])
    signals = json.loads(args.signals.read_text(encoding="utf-8"))
    result = run_manual_daily_portfolio(daily=pd.read_parquet(args.bars), signals=signals, calendar=calendar, bundle=bundle, start=date.fromisoformat(args.start), end=date.fromisoformat(args.end))
    result.write(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
