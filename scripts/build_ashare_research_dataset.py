"""Validate public source archives and build typed, hashed research inputs."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from quant_engine.data.calendar import TradingCalendar
from scripts.acquire_ashare_research import canonical, digest, write_json


def reference_return_prices(frame: pd.DataFrame) -> pd.Series:
    """PIT reference-return proxy, not shareholder cash-account wealth.

    Invalid references stop the chain instead of silently bridging a bad bar.
    The caller separately verifies calendar/lifecycle continuity.
    """
    close = pd.to_numeric(frame.close, errors="coerce")
    reference = pd.to_numeric(frame.preclose, errors="coerce")
    valid = np.isfinite(close) & (close > 0) & np.isfinite(reference) & (reference > 0)
    ratios = (close / reference).where(valid)
    if not len(ratios):
        return ratios
    ratios.iloc[0] = 1.0 if valid.iloc[0] else np.nan
    return ratios.cumprod(skipna=False) * close.iloc[0]


def normalize_daily(frame: pd.DataFrame, factors: pd.DataFrame, code: str, signal_mode: str = "vendor_factor") -> pd.DataFrame:
    """Keep source gaps; attach only factors effective on or before each date."""
    result = frame.copy()
    result["date"] = pd.to_datetime(result["date"], errors="raise")
    if result.date.duplicated().any():
        raise ValueError(f"duplicate daily date: {code}")
    result = result.sort_values("date").reset_index(drop=True)
    result["code"] = canonical(code)
    for field in ("open", "high", "low", "close", "preclose", "volume", "amount", "turn"):
        result[field] = pd.to_numeric(result[field], errors="coerce")
    if not result["tradestatus"].isin(["0", "1"]).all() or not result["isST"].isin(["0", "1"]).all():
        raise ValueError(f"unknown explicit trading status: {code}")
    result["is_suspended"] = result["tradestatus"] == "0"
    result["is_st"] = result["isST"] == "1"
    result["turnover_rate"] = result["turn"] / 100.0
    multiplier = pd.Series(1.0, index=result.index)
    if not factors.empty:
        events = factors[["dividOperateDate", "backAdjustFactor"]].copy()
        events.columns = ["factor_date", "factor"]
        events["factor_date"] = pd.to_datetime(events.factor_date, errors="raise")
        events["factor"] = pd.to_numeric(events.factor, errors="raise")
        if not (np.isfinite(events.factor) & (events.factor > 0)).all():
            raise ValueError(f"invalid adjustment factor: {code}")
        events = events.drop_duplicates().sort_values("factor_date")
        if events.factor_date.duplicated().any():
            raise ValueError(f"conflicting factors: {code}")
        multiplier = pd.merge_asof(result[["date"]], events, left_on="date", right_on="factor_date", direction="backward")["factor"].fillna(1.0)
    result["vendor_adjusted_close"] = result["close"] * multiplier
    result["source_return"] = pd.to_numeric(result.get("pctChg", pd.Series(np.nan, index=result.index)), errors="coerce") / 100
    if signal_mode not in {"vendor_factor", "reference_chain"}:
        raise ValueError("unknown signal adjustment mode")
    result["adjusted_close"] = reference_return_prices(result) if signal_mode == "reference_chain" else result["vendor_adjusted_close"]
    return result[["code", "date", "open", "high", "low", "close", "preclose", "volume", "amount",
                   "turnover_rate", "is_suspended", "is_st", "adjusted_close", "vendor_adjusted_close", "source_return"]]


def normalize_actions(frame: pd.DataFrame, code: str, end: str, resolutions: list | None = None) -> pd.DataFrame:
    columns = ["code", "record_date", "ex_date", "pay_date", "stock_date", "cash_ps", "bonus_ratio"]
    records = []
    # The API can repeat an event across operate-year responses with one
    # response omitting numeric fields. Coalesce compatible known values
    # before defaults; interpreting the blank copy as zero fabricates a
    # conflicting event or silently drops the actual dividend.
    numeric_keys = ("dividCashPsBeforeTax", "dividStocksPs", "dividReserveToStockPs")
    date_keys = ("dividRegistDate", "dividPayDate", "dividStockMarketDate")
    merged_rows = []
    if frame.empty:
        return pd.DataFrame(columns=columns)
    for ex_date, group in frame.groupby("dividOperateDate", dropna=False):
        merged = {"dividOperateDate": ex_date}
        for key in (*numeric_keys, *date_keys):
            present = [value for value in group[key] if pd.notna(value) and str(value).strip()] if key in group else []
            values = {float(value) if key in numeric_keys else str(value) for value in present}
            if len(values) > 1:
                resolution = next((item for item in resolutions or [] if item["code"] == canonical(code)
                                   and item["ex_date"] == ex_date and item.get("source_url")), None)
                if (key == "dividCashPsBeforeTax" and resolution
                        and set(resolution["cash_parts"]) == values
                        and abs(sum(values) - float(resolution["cash_ps"])) < 1e-9):
                    values = {float(resolution["cash_ps"])}
                else:
                    raise ValueError(f"conflicting corporate action versions: {code} {ex_date} {key}")
            merged[key] = next(iter(values)) if values else None
        merged_rows.append(merged)
    for row in merged_rows:
        ex = row.get("dividOperateDate")
        if not ex or ex > end:
            continue  # unimplemented proposals are not account events
        date.fromisoformat(ex)
        def number(key):
            value = row.get(key)
            parsed = 0.0 if value in (None, "") else float(value)
            if not np.isfinite(parsed) or parsed < 0:
                raise ValueError(f"invalid company action {code}: {key}")
            return parsed
        def event_date(key):
            value = row.get(key)
            if not value:
                return None
            date.fromisoformat(value)
            return value
        records.append({"code": canonical(code), "record_date": event_date("dividRegistDate"),
                        "ex_date": ex, "pay_date": event_date("dividPayDate"),
                        "stock_date": event_date("dividStockMarketDate"),
                        "cash_ps": number("dividCashPsBeforeTax"),
                        "bonus_ratio": number("dividStocksPs") + number("dividReserveToStockPs")})
    result = pd.DataFrame(records, columns=columns).drop_duplicates()
    if not result.empty and result.duplicated(["code", "ex_date"]).any():
        raise ValueError(f"conflicting corporate action versions: {code}")
    return result


def security_quality(daily: pd.DataFrame, actions: pd.DataFrame, metadata: dict,
                     sessions: pd.DatetimeIndex) -> dict:
    code = metadata["code"]
    ipo = pd.Timestamp(metadata["ipo_date"])
    out = pd.Timestamp(metadata["out_date"]) if metadata.get("out_date") else pd.Timestamp.max
    expected = sessions[(sessions >= ipo) & (sessions < out)]
    missing = expected.difference(daily.date)
    trading = daily[~daily.is_suspended]
    values = trading[["open", "high", "low", "close", "preclose", "volume", "amount"]]
    invalid = (~np.isfinite(values).all(axis=1) | (values[["open", "high", "low", "close", "preclose"]] <= 0).any(axis=1)
               | (values[["volume", "amount"]] < 0).any(axis=1)
               | (values.high < values[["open", "close", "low"]].max(axis=1))
               | (values.low > values[["open", "close", "high"]].min(axis=1)))
    # Quantized OHLC bounds permit a price*volume audit of source units.
    positive_volume = trading.volume > 0
    vwap = trading.amount / trading.volume.where(positive_volume)
    unit_error = positive_volume & ((vwap < trading.low * .97) | (vwap > trading.high * 1.03))
    adjustments = []
    by_ex = {row["ex_date"]: row for row in actions.to_dict("records")}
    previous_close = daily.close.where(daily.close > 0).ffill().shift(1)
    for index in daily.index:
        previous = previous_close.loc[index]
        reference = daily.loc[index, "preclose"]
        if not np.isfinite(previous) or not np.isfinite(reference) or reference <= 0:
            continue
        day = daily.loc[index, "date"].date().isoformat()
        action = by_ex.get(day)
        theoretical = ((previous - action["cash_ps"]) / (1 + action["bonus_ratio"])) if action else previous
        if abs(theoretical - reference) > max(.025, abs(reference) * .0002):
            adjustments.append({"date": day, "previous_close": float(previous),
                                "reference": float(reference), "expected_reference": float(theoretical),
                                "action_present": action is not None})
    reference_return = daily.close / daily.preclose - 1
    mismatch = (daily.vendor_adjusted_close.pct_change(fill_method=None) - reference_return).abs() > 1e-5
    source_mismatch = (daily.source_return - reference_return).abs() > 1e-5
    return {"code": code, "rows": len(daily), "expected_sessions": len(expected),
            "missing_count": len(missing), "missing_dates": [d.date().isoformat() for d in missing],
            "invalid_trading_rows": int(invalid.sum()), "price_volume_amount_outliers": int(unit_error.sum()),
            "suspension_rows": int(daily.is_suspended.sum()), "st_rows": int(daily.is_st.sum()),
            "out_date": metadata.get("out_date"), "corporate_action_count": len(actions),
            "unexplained_reference_adjustments": adjustments,
            "factor_return_mismatch_dates": daily.loc[mismatch, "date"].dt.strftime("%Y-%m-%d").tolist(),
            "source_return_mismatch_dates": daily.loc[source_mismatch, "date"].dt.strftime("%Y-%m-%d").tolist()}


def merge_supplemental_actions(actions: pd.DataFrame, supplements: list[dict], code: str) -> pd.DataFrame:
    """Add sourced missing events or fill unknown dates, never replace facts."""
    rows = actions.to_dict("records")
    by_date = {row["ex_date"]: row for row in rows}
    for item in supplements:
        if item["code"] != canonical(code):
            continue
        if not item.get("source_url") or len(item.get("source_sha256", "")) != 64:
            raise ValueError("supplemental action requires source URL and archived content hash")
        ex = date.fromisoformat(item["ex_date"]).isoformat()
        for field in ("cash_ps", "bonus_ratio"):
            if not np.isfinite(float(item[field])) or float(item[field]) < 0:
                raise ValueError(f"invalid supplemental {field}")
        for field in ("record_date", "pay_date", "stock_date"):
            if item.get(field):
                date.fromisoformat(item[field])
        if ex not in by_date:
            row = {key: item.get(key) for key in actions.columns}
            rows.append(row)
            by_date[ex] = row
            continue
        row = by_date[ex]
        for field in ("cash_ps", "bonus_ratio"):
            if abs(float(row[field]) - float(item[field])) > 1e-8:
                raise ValueError(f"supplement conflicts with source amount: {code} {ex} {field}")
        for field in ("record_date", "pay_date", "stock_date"):
            if row.get(field) and item.get(field) and row[field] != item[field]:
                raise ValueError(f"supplement conflicts with source date: {code} {ex} {field}")
            if not row.get(field):
                row[field] = item.get(field)
    return pd.DataFrame(rows, columns=actions.columns).sort_values("ex_date").reset_index(drop=True)


def build(root: Path, version: str = "normalized-v1", signal_mode: str = "reference_chain") -> dict:
    if (root / version / "manifest.json").exists():
        raise ValueError("normalized dataset is frozen; use a new --version to preserve existing evidence")
    acquisition = json.loads((root / "acquisition.json").read_text())
    if not acquisition["complete"]:
        raise ValueError("acquisition is incomplete; resume failed source requests first")
    selection = acquisition["selection"]
    calendar = TradingCalendar(start_year=int(selection["data_start"][:4]), end_year=int(selection["data_end"][:4]))
    evidence = calendar.coverage_report(date.fromisoformat(selection["data_start"]), date.fromisoformat(selection["data_end"]))
    if not evidence["complete"]:
        raise ValueError("verified trading calendar required")
    sessions = pd.DatetimeIndex(evidence["trading_days"])
    master = pd.read_parquet(root / "source/stock_basic.parquet").set_index("code")
    daily_frames, action_frames, securities, quality, source_files = [], [], [], [], []
    resolution_path = Path("docs/research-runs/ashare-action-resolutions.json")
    resolutions = json.loads(resolution_path.read_text())["resolutions"] if resolution_path.exists() else []
    if resolution_path.exists():
        source_files.append({"path": str(resolution_path), "sha256": digest(resolution_path)})
    supplemental_path = root / "source/supplemental_actions.json"
    supplements = json.loads(supplemental_path.read_text())["actions"] if supplemental_path.exists() else []
    if supplemental_path.exists():
        source_files.append({"path": supplemental_path.relative_to(root).as_posix(), "sha256": digest(supplemental_path)})
        for receipt in sorted((root / "source/sina_actions").rglob("*.html")):
            metadata = json.loads(receipt.with_suffix(".json").read_text())
            actual_hash = digest(receipt)
            if actual_hash != metadata["sha256"]:
                raise ValueError(f"supplemental raw receipt changed: {receipt}")
            source_files.append({"path": receipt.relative_to(root).as_posix(), "sha256": actual_hash})
    for code in selection["codes"]:
        base = root / "source/securities" / code
        files = sorted(base.rglob("*.parquet"))
        for file in files:
            info = json.loads(file.with_suffix(".json").read_text())
            actual = digest(file)
            if actual != info["sha256"]:
                raise ValueError(f"source archive changed: {file}")
            source_files.append({"path": file.relative_to(root).as_posix(), "sha256": actual})
        daily_path = base / "daily-repair-v2.parquet" if (base / "daily-repair-v2.parquet").exists() else base / "daily.parquet"
        frame = normalize_daily(pd.read_parquet(daily_path), pd.read_parquet(base / "factors.parquet"), code, signal_mode)
        dividends = pd.concat([pd.read_parquet(path) for path in sorted((base / "dividends").glob("*.parquet"))], ignore_index=True)
        actions = normalize_actions(dividends, code, selection["data_end"], resolutions)
        actions = merge_supplemental_actions(actions, supplements, code)
        # Risk-warning capital restructurings can allocate new shares to
        # creditors/investors instead of existing public shareholders. The
        # headline capitalization ratio is not proof of holder entitlement.
        st_by_day = frame.set_index("date").is_st
        actions["bonus_allocation_verified"] = [
            not (float(row["bonus_ratio"]) > 0 and bool(st_by_day.get(pd.Timestamp(row["ex_date"]), True)))
            for row in actions.to_dict("records")
        ]
        stock = master.loc[code]
        metadata = {"code": canonical(code), "name": stock.code_name,
                    "ipo_date": stock.ipoDate, "out_date": stock.outDate or None}
        securities.append(metadata)
        daily_frames.append(frame)
        action_frames.append(actions)
        quality.append(security_quality(frame, actions, metadata, sessions))
    output = root / version
    output.mkdir(parents=True, exist_ok=True)
    daily = pd.concat(daily_frames, ignore_index=True).sort_values(["date", "code"])
    actions = pd.concat(action_frames, ignore_index=True).sort_values(["ex_date", "code"])
    for name, frame in (("daily", daily), ("actions", actions), ("securities", pd.DataFrame(securities))):
        frame.to_parquet(output / f"{name}.parquet", index=False)
    pd.DataFrame({"date": sessions}).to_parquet(output / "calendar.parquet", index=False)
    limits = ["2014 inception main-board sample, not all A-shares or dynamic index membership",
              "public source revisions and historic delisting liquidation proceeds are not fully verified",
              "rights subscriptions are absent from the dividend API and must remain unresolved when detected",
              "signal reference-return proxy and real cash/share corporate actions use separate accounting",
              "ST capitalization requires independent verification of allocation to existing shareholders",
              "dividend tax and transfer fee scenarios are explicit research assumptions"]
    payload = {"dataset_id": f"{selection['dataset_id']}/{version}", "universe_count": len(securities),
               "start_date": selection["data_start"], "end_date": selection["data_end"],
               "inception": selection["inception"], "universe": selection["universe"],
               "signal_adjustment": signal_mode,
               "source": "baostock:raw_daily+stock_basic+dividend+adjust_factor",
               "selection": selection, "calendar": evidence,
               "files": {name: {"path": str((output / f"{name}.parquet").resolve()), "sha256": digest(output / f"{name}.parquet")}
                         for name in ("daily", "actions", "securities", "calendar")},
               "source_files": source_files, "quality": quality, "limitations": limits}
    # Portable content identity excludes local absolute paths and receipt time.
    identity = {"universe": payload["universe"], "start_date": payload["start_date"],
                "end_date": payload["end_date"], "calendar_hash": evidence["content_hash"],
                "files": {key: value["sha256"] for key, value in payload["files"].items()},
                "source_files": source_files}
    canonical_json = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    payload["content_hash"] = hashlib.sha256(canonical_json.encode()).hexdigest()
    write_json(output / "manifest.json", payload)
    print(json.dumps({"dataset_id": payload["dataset_id"], "rows": len(daily), "actions": len(actions),
                      "missing": sum(q["missing_count"] for q in quality),
                      "invalid_rows": sum(q["invalid_trading_rows"] for q in quality),
                      "unexplained_actions": sum(len(q["unexplained_reference_adjustments"]) for q in quality)}, ensure_ascii=False), flush=True)
    return payload


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("data/research/ashare-inception-2014-v1"))
    parser.add_argument("--version", default="normalized-v1")
    parser.add_argument("--signal-mode", choices=["reference_chain", "vendor_factor"], default="reference_chain")
    args = parser.parse_args()
    build(args.root, args.version, args.signal_mode)
