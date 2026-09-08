"""Run the point-in-time factor generation and de-correlation workflow."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from quant_engine.data.api import DataAPI
from quant_engine.factor import Factor, compute_factor
from quant_engine.factor.research import run_factor_research

RAW_BASELINE_FACTORS = (
    "raw_momentum_lagged_20_close",
    "raw_realized_volatility_20_close",
    "raw_turnover_mean_20_turnover",
)


def _json_number(value: float | None) -> float | None:
    if value is None:
        return None
    numeric = float(value)
    return numeric if np.isfinite(numeric) else None


def build_forward_open_returns(open_prices: pd.DataFrame, holding_period: int) -> pd.DataFrame:
    """Label a close signal by its executable next-open to later-open return."""
    if holding_period < 1:
        raise ValueError("holding period must be >= 1")
    result = pd.DataFrame(np.nan, index=open_prices.index, columns=open_prices.columns)
    exit_offset = holding_period + 1
    if len(open_prices) > exit_offset:
        result.iloc[:-exit_offset, :] = (
            open_prices.iloc[exit_offset:, :].to_numpy()
            / open_prices.iloc[1:-holding_period, :].to_numpy()
            - 1.0
        )
    return result


def compute_existing_factor_baselines(data: pd.DataFrame) -> dict[str, pd.Series]:
    """Compute project built-ins plus core raw exposures for correlation gates."""
    result = {name: compute_factor(name, data) for name in RAW_BASELINE_FACTORS}
    for name in Factor.list_registered():
        factor_type = Factor.get(name)
        if factor_type is None or not set(factor_type.inputs).issubset(data.columns):
            continue
        parts: list[pd.Series] = []
        for code, group in data.groupby(level="code", sort=False):
            single_asset = group.droplevel("code")
            values = factor_type().compute(single_asset)
            values.index = pd.MultiIndex.from_arrays(
                [[code] * len(values), values.index], names=["code", "date"]
            )
            parts.append(values)
        result[f"builtin:{name}"] = pd.concat(parts).sort_index()
    return result


def run(
    *,
    start: date,
    end: date,
    output_dir: Path,
    codes: list[str] | None = None,
    forward_horizon: int = 5,
    correlation_limit: float = 0.70,
) -> dict:
    api = DataAPI()
    inventory = api.daily_inventory()
    available = {str(item["code"]): item for item in inventory}
    selected = codes or sorted(available)
    missing = sorted(set(selected) - set(available))
    if missing:
        raise ValueError(f"codes absent from local daily inventory: {', '.join(missing)}")
    if len(selected) < 3:
        raise ValueError("factor research requires at least three securities")

    fields = ["open", "high", "low", "close", "volume", "amount", "turnover_rate"]
    data = api.daily(selected, start, end, fields=fields, adjust="event_driven")
    if data.empty:
        raise ValueError("local daily data is empty for the requested sample")
    baselines = compute_existing_factor_baselines(data)
    open_prices = data["open"].unstack("code").sort_index()
    labels = build_forward_open_returns(open_prices, forward_horizon)
    result = run_factor_research(
        data,
        existing_factors=baselines,
        forward_returns=labels,
        correlation_limit=correlation_limit,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    factor_rows = pd.concat(
        {
            name: values.stack(future_stack=True).rename("value")
            for name, values in result.values.items()
        },
        names=["factor"],
    ).reset_index()
    factor_rows.to_parquet(output_dir / "candidate_factors.parquet", index=False)
    result.correlation_matrix.to_csv(output_dir / "correlations.csv")

    data_hash = hashlib.sha256(
        pd.util.hash_pandas_object(data.sort_index(), index=True).values.tobytes()
    ).hexdigest()
    report = {
        "workflow_version": "factor-research-v1",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "codes": selected,
        "code_count": len(selected),
        "row_count": len(data),
        "input_hash": data_hash,
        "adjustment": "event_driven",
        "forward_horizon": forward_horizon,
        "forward_return_definition": "signal_t_close; entry_t_plus_1_open; exit_t_plus_1_plus_h_open",
        "correlation_limit": correlation_limit,
        "baseline_factors": list(baselines),
        "accepted": list(result.accepted),
        "evidence": [
            {
                "name": item.name,
                "accepted": item.accepted,
                "decision_reason": item.decision_reason,
                "maximum_absolute_correlation": _json_number(
                    item.maximum_absolute_correlation
                ),
                "most_correlated_factor": item.most_correlated_factor,
                "ic_summary": {
                    key: _json_number(value)
                    for key, value in item.ic_summary.items()
                } if item.ic_summary is not None else None,
            }
            for item in result.evidence
        ],
        "limitations": [
            "Acceptance only means the measured correlation gate passed; it is not a profitability claim.",
            "Forward returns are research labels and are never available to deployable factor functions.",
            "The local inventory is not a point-in-time historical universe and may contain survivorship bias.",
            "Fundamental candidates require an imported PIT announcement history and are not evaluated here.",
        ],
    }
    (output_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, default=date(2018, 1, 2))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2026, 8, 31))
    parser.add_argument("--codes", help="comma-separated local security codes")
    parser.add_argument("--forward-horizon", type=int, default=5)
    parser.add_argument("--correlation-limit", type=float, default=0.70)
    parser.add_argument("--output", type=Path, default=Path("factor_research_result"))
    args = parser.parse_args()
    report = run(
        start=args.start,
        end=args.end,
        output_dir=args.output,
        codes=[item.strip().upper() for item in args.codes.split(",") if item.strip()]
        if args.codes else None,
        forward_horizon=args.forward_horizon,
        correlation_limit=args.correlation_limit,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
