"""Combine immutable rounds without selecting only profitable experiments."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from scripts.acquire_ashare_research import write_json

STAGE_ORDER = {"discovery": 0, "validation": 1, "oos": 2, "full": 3}
NOTES = [
    {"title": "研究范围与执行假设", "url": "", "text": "正式回测2015-01-05至2026-08-31；2013起数据仅供预热。2014历史主板池固定抽500只，包含37只后来有退市日期的证券。每个分段从100万元重新开始，全段连续持仓，因此不能简单相乘分段收益替代全段。"},
    {"title": "原价成交与参考收益信号", "url": "https://www.baostock.com/mainContent?file=stockKData.md", "text": "原价成交与股数、登记/应收/派息/红股上市分开核算；信号使用当日close/preclose参考收益链，不等同现金分红账户财富。价格指数基准不含分红。"},
    {"title": "低波证据与适配", "url": "https://link.springer.com/article/10.1057/s41260-021-00218-0", "text": "原研究是36个月月收益波动率；本项目252日等权为适配，60日为预注册敏感性。不存在将全样本beta缩放用作可交易杠杆。"},
    {"title": "反转证据与适配", "url": "https://rodneywhitecenter.wharton.upenn.edu/wp-content/uploads/2019/04/01-18-Stambaugh.Published.BlumePrize.pdf", "text": "原始20日反转每天调五分之一、每份持5日；项目周频等权不是精确复制。传统动量作为A股证据较弱的探索对照。"},
    {"title": "近期反例", "url": "https://www.msci.com/documents/10199/9824547d-f100-4eb8-bcd5-ff1f0ef4af57", "text": "MSCI截至2026-08-31资料显示低波并非每年跑赢母指数。历史论文、优化指数和本项目多头适配不能混为同一种策略。"},
    {"title": "留出样本边界", "url": "", "text": "参数在真实绩效前固定；2023-2026为本项目时间留出，但参考文献样本部分重合，不能称完全独立于全部先验研究。窗口敏感性只在发现/验证期运行。"},
    {"title": "费用与指标", "url": "", "text": "基线/压力同时改变佣金、滑点、昨日成交量参与率与股息税假设。CAGR按真实日历跨度，波动和Sharpe按252交易日且rf=0。年化双边成交额/净值不等同单边换手；显式费用不另加已包含于成交价的滑点。"},
    {"title": "解释限制", "url": "", "text": "现金、应收与股数重放通过只证明账务一致；不能消除未核实配股/重整/清算事件或初始样本范围限制。未通过完整源验证的结果保持研究状态，不授权自动观察或真实交易。"},
]


def combine(paths: list[Path], output: Path) -> dict:
    rounds = [json.loads(path.read_text()) for path in paths]
    identities = {item["dataset"]["content_hash"] for item in rounds}
    if len(identities) != 1:
        raise ValueError("cannot silently combine different datasets into one final study")
    result = {**rounds[0], "runs": [], "rounds": [str(path.resolve()) for path in paths], "research_notes": NOTES}
    seen = set()
    rows, annual = [], []
    for study in rounds:
        for run in study["runs"]:
            key = (run["strategy_id"], run["period"], run["cost_scenario"], json.dumps(run["parameters"], sort_keys=True))
            if key in seen:
                raise ValueError(f"duplicate experiment definition: {key}")
            seen.add(key)
            result["runs"].append(run)
            rows.append({"run_id": run["run_id"], "label": run["label"], "strategy": run["strategy_id"],
                         "period": run["period"], "cost_scenario": run["cost_scenario"], "validated": run["validated"],
                         "status": run["status"], "parameters": json.dumps(run["parameters"], ensure_ascii=False, sort_keys=True),
                         **run.get("metrics", {})})
            if run["status"] != "completed":
                continue
            frame = pd.read_parquet(Path(run["result_dir"]) / "daily_portfolio.parquet")
            frame["date"] = pd.to_datetime(frame.date)
            year_returns = frame.groupby(frame.date.dt.year).daily_return.apply(lambda x: float(np.prod(1 + x) - 1))
            if not np.isclose(np.prod(1 + year_returns) - 1, run["metrics"]["total_return"], atol=1e-8):
                raise ValueError("annual return groups do not reconcile with total return")
            for year, value in year_returns.items():
                segment = frame[frame.date.dt.year == year]
                annual.append({"run_id": run["run_id"], "label": run["label"], "period": run["period"],
                               "cost_scenario": run["cost_scenario"], "year": int(year), "return": float(value),
                               "start_date": str(segment.date.min().date()), "end_date": str(segment.date.max().date())})
    result["runs"].sort(key=lambda run: (STAGE_ORDER[run["period"]], run["label"]))
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "matrix.json", result)
    pd.DataFrame(rows).to_csv(output / "experiment-metrics.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(annual).to_csv(output / "annual-returns.csv", index=False, encoding="utf-8-sig")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--matrices", nargs="+", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    result = combine(arguments.matrices, arguments.output)
    print(json.dumps({"runs": len(result["runs"]), "completed": sum(run["status"] == "completed" for run in result["runs"])}))
