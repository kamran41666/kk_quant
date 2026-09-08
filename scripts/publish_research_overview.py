"""Create a concise local report entry from audited, immutable matrix files."""
from __future__ import annotations

import argparse
from html import escape
import json
from pathlib import Path
import shutil

from scripts.acquire_ashare_research import write_json


def publish(matrix_path: Path, output: Path) -> Path:
    matrix = json.loads(matrix_path.read_text())
    audited = []
    for path in matrix["rounds"]:
        audit = json.loads((Path(path).parent / "accounting-audit.json").read_text())
        if not audit["all_completed_runs_passed"]:
            raise ValueError("accounting replay has unresolved failures")
        audited.extend(audit["runs"])
    if {row["run_id"] for row in audited} != {row["run_id"] for row in matrix["runs"]}:
        raise ValueError("accounting audit does not cover exactly the final experiment set")
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "accounting-audit.json", {"passed": True, "experiments": len(audited),
               "scope": "accounting_consistency_only", "runs": audited})
    for filename in ("matrix.json", "experiment-metrics.csv", "annual-returns.csv", "reproducibility.json"):
        shutil.copyfile(matrix_path.parent / filename, output / filename)
    names = {"research-low-volatility": "低波动（252日）", "research-short-reversal": "短期反转（20日）", "research-momentum": "动量（252−21日）"}
    def get(strategy, period, cost):
        return next(row for row in matrix["runs"] if row["strategy_id"] == strategy and row["period"] == period and row["cost_scenario"] == cost and "默认窗口" in row["label"])
    def percent(value):
        return f"{value:+.2%}"
    table, figures = [], []
    for strategy, label in names.items():
        full, oos, stress = get(strategy, "full", "baseline"), get(strategy, "oos", "baseline"), get(strategy, "full", "stress")
        metrics = full["metrics"]
        table.append(f"<tr><th>{escape(label)}</th><td>{percent(metrics['total_return'])}</td><td>{percent(metrics['annual_return'])}</td><td>{percent(metrics['max_drawdown'])}</td><td>{percent(oos['metrics']['total_return'])}</td><td>{percent(stress['metrics']['total_return'])}</td></tr>")
        index = matrix["runs"].index(full) + 1
        figure = f"run-{index:03d}.png"
        if not (output / figure).exists():
            raise ValueError(f"missing rendered figure: {figure}")
        figures.append(f'<section><div class="section-top"><h2>{escape(label)} · 长周期基线</h2><a href="{figure}" download>下载 PNG ↗</a></div><img src="{figure}" alt="{escape(label)}的真实策略净值、上证指数与沪深300对照及最大上涨回撤区间"></section>')
    document = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>kk_quant · A股深度回测</title><style>
*{box-sizing:border-box}body{margin:0;background:#f2f5f9;color:#1b2d43;font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}main{max-width:1440px;margin:auto;padding:44px 28px}header{padding:30px 32px;border-radius:18px;background:#132538;color:#f5f9ff}.kicker{font:11px monospace;letter-spacing:3px;color:#74d7cf}h1{font-size:32px;line-height:1.3;margin:14px 0}header p{color:#b7c7d7;line-height:1.8}.meta{display:flex;flex-wrap:wrap;gap:12px;margin-top:22px}.meta span{padding:8px 12px;border:1px solid #334659;border-radius:8px;font-size:12px}.links{display:flex;flex-wrap:wrap;gap:10px;margin:24px 0}a{color:#24679a;text-decoration:none}.links a{background:white;border:1px solid #dbe4ef;border-radius:8px;padding:10px 14px;font-size:13px}section{background:white;padding:24px;margin-top:22px;border:1px solid #dce4ed;border-radius:12px}h2{font-size:19px;margin:0 0 14px}p,li{line-height:1.8}.warning{background:#fff3d9;border-left:3px solid #d69d32;padding:14px 18px;color:#795822;margin:18px 0;font-size:13px}.table-wrap{overflow:auto}table{width:100%;border-collapse:collapse;font-size:13px;white-space:nowrap}th,td{padding:14px 12px;border-bottom:1px solid #e5ebf2;text-align:right;font-variant-numeric:tabular-nums}th:first-child{text-align:left}thead{color:#607386}small,.muted{color:#697b8e;line-height:1.7}img{display:block;width:100%;height:auto}.section-top{display:flex;justify-content:space-between;align-items:center;gap:12px}.section-top a{font-size:12px}.hash{overflow-wrap:anywhere;font:11px monospace;line-height:1.8}footer{margin:28px 0;color:#61758a;font-size:12px;line-height:1.9}@media(max-width:640px){main{padding:18px 12px}header{padding:22px 20px}h1{font-size:25px}section{padding:16px}.section-top{align-items:flex-start;flex-direction:column}}
</style><main><header><div class="kicker">KK_QUANT / A-SHARE RESEARCH</div><h1>三种策略，十一年多真实数据检验</h1><p>2015-01-05 — 2026-08-31 · 沪深主板历史起点样本 · 原价成交与公司行动账本</p><div class="meta"><span>500 只证券</span><span>1,625,850 行原始行情</span><span>36 组正式实验</span><span>36 / 36 账务重放通过</span><span>12 组复跑完全一致</span></div></header>
<nav class="links"><a href="index.html">全部36组图表与研究来源 ↗</a><a href="../ashare-research-data.xlsx" download>下载Excel明细</a><a href="experiment-metrics.csv" download>实验指标CSV</a><a href="annual-returns.csv" download>分年度收益CSV</a><a href="accounting-audit.json" download>独立账务审计</a><a href="reproducibility.json" download>复跑一致性记录</a></nav>
<section><h2>筛选结论</h2><p><strong>低波动是本轮最值得继续验证的候选。</strong>它在发现、验证和时间留出三段的基线收益均为正；普通动量与短期反转在时间留出段为负，反转受到明显的交易成本侵蚀。本轮不把未通过的候选包装成有效策略。</p><div class="warning">全部36组仍保留“未通过完整验证”：部分配股、差异化分红、重整受益资格及真实清算回收值尚未完全核实。账务一致不等于数据/市场事件完整，也不等于当前可部署；没有策略获自动观察或真实交易授权。</div><div class="table-wrap"><table><thead><tr><th>候选</th><th>全段总收益</th><th>年化CAGR</th><th>最大回撤</th><th>2023—2026.08留出收益</th><th>全段压力总收益</th></tr></thead><tbody>'''
    document += "".join(table)
    document += '''</tbody></table></div><p class="muted">表内为冻结参数下的模型结果。基线与压力包含不同佣金、滑点、昨日成交量参与率及股息税假设；并非真实券商逐笔费率。分段账户重置为100万元，全段持续持仓，因此不直接相乘分段收益。</p></section>'''
    document += "".join(figures)
    document += f'<footer>原始数据从2013年起提供预热；正式实验从2015年开始。初始股票池来自2014历史名单，保留37只后来有退市日期的证券；并非全A股或动态指数成分。上证与沪深300为不含分红的价格指数，不能从曲线差直接推断独立因子Alpha。<p class="hash">数据内容哈希：{escape(matrix["dataset"]["content_hash"])}</p>研究假设、原始论文与来源链接见完整报告。静态图抽检了三个长周期基线及关键风险图；数值由原始结果与独立账务重放核对。</footer></main></html>'
    target = output / "overview.html"
    target.write_text(document, encoding="utf-8")
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(publish(args.matrix, args.output))
