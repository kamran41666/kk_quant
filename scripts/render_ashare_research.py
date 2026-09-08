"""Render audited A-share matrices into portable HTML and PNG reports.

Returns and intervals describe recorded backtests, not evidence of live profitability.
Run: python scripts/render_ashare_research.py --matrix MATRIX --output-dir OUTPUT
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from dataclasses import dataclass
from html import escape
from pathlib import Path
from urllib.parse import urlsplit

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib import font_manager

REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class ReturnInterval:
    start: int
    end: int
    change: float


def _positive_values(values: Sequence[float]) -> np.ndarray:
    result = np.asarray(values, dtype=float)
    if result.ndim != 1 or result.size == 0:
        raise ValueError("净值必须是非空一维序列")
    if not np.isfinite(result).all() or (result <= 0).any():
        raise ValueError("净值必须是有限正数；不能忽略缺失值后计算区间")
    return result


def maximum_rise(values: Sequence[float]) -> ReturnInterval | None:
    """Largest later / earlier - 1, requiring a trough before a later peak.

    Return None when there is no strictly positive rise. Equal extrema keep the
    first encountered pair. Index order, not independently sorted prices, matters.
    """
    nav = _positive_values(values)
    low, best = 0, None
    for end in range(1, len(nav)):
        change = float(nav[end] / nav[low] - 1)
        if change > 0 and (best is None or change > best.change):
            best = ReturnInterval(low, end, change)
        if nav[end] < nav[low]:
            low = end
    return best


def maximum_drawdown(values: Sequence[float]) -> ReturnInterval | None:
    """Largest peak-to-later-trough loss, returned as a negative change."""
    nav = _positive_values(values)
    peak, best = 0, None
    for end in range(1, len(nav)):
        change = float(nav[end] / nav[peak] - 1)
        if change < 0 and (best is None or change < best.change):
            best = ReturnInterval(peak, end, change)
        if nav[end] > nav[peak]:
            peak = end
    return best


def _dated_frame(path: Path, columns: list[str]) -> pd.DataFrame:
    frame = pd.read_parquet(path)
    missing = {"date", *columns} - set(frame.columns)
    if missing:
        raise ValueError(f"{path.name} 缺少字段 {sorted(missing)}")
    frame = frame[["date", *columns]].copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="raise").dt.normalize()
    if frame.empty or frame["date"].isna().any() or frame["date"].duplicated().any():
        raise ValueError(f"{path.name} 日期为空、缺失或重复")
    return frame.sort_values("date").set_index("date")


def normalize_benchmark(frame: pd.DataFrame, dates: pd.DatetimeIndex) -> pd.Series:
    """Normalize to the exact run start close; missing observations stay NaN.

    If the run start has no valid close, no baseline can be established. Do not
    silently move the baseline forward or fill any prices across missing days.
    """
    values = pd.to_numeric(frame["close"], errors="coerce").reindex(dates)
    values = values.where(np.isfinite(values) & (values > 0))
    if len(values) == 0 or pd.isna(values.iloc[0]):
        return pd.Series(np.nan, index=dates, dtype=float)
    return values / values.iloc[0]


def _font() -> None:
    candidates = [
        Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
        Path("/System/Library/Fonts/STHeiti Medium.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    ]
    for path in candidates:
        if path.exists():
            font_manager.fontManager.addfont(str(path))
            plt.rcParams["font.family"] = font_manager.FontProperties(
                fname=str(path)
            ).get_name()
            break
    plt.rcParams["axes.unicode_minus"] = False


def _resolve(value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else REPO_ROOT / path


def _text(value: object) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _metric(value: object, percent: bool = False) -> str:
    try:
        number = float(value)
    except (ValueError, TypeError):
        return "—"
    if not np.isfinite(number):
        return "—"
    return f"{number:.2%}" if percent else f"{number:,.2f}"


def _interval_label(
    name: str, interval: ReturnInterval | None, dates: pd.DatetimeIndex
) -> str:
    if interval is None:
        return f"{name}：无"
    return (
        f"{name} {interval.change:+.2%}  |  "
        f"{dates[interval.start]:%Y-%m-%d} → {dates[interval.end]:%Y-%m-%d}"
    )


def render_run(
    run: dict, benchmarks: dict[str, pd.DataFrame], output: Path
) -> list[str]:
    directory = _resolve(run["result_dir"])
    daily = _dated_frame(
        directory / "daily_portfolio.parquet", ["total_value", "daily_return"]
    )
    summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    initial = float(summary["initial_capital"])
    if not np.isfinite(initial) or initial <= 0:
        raise ValueError("initial_capital 必须是有限正数")
    nav = _positive_values(
        pd.to_numeric(daily["total_value"], errors="raise").to_numpy() / initial
    )
    dates = pd.DatetimeIndex(daily.index)
    rise, drawdown = maximum_rise(nav), maximum_drawdown(nav)
    notes: list[str] = []
    # Include benchmark dates to expose missing strategy rows as actual gaps.
    grid = dates
    for frame in benchmarks.values():
        grid = grid.union(
            frame.index[(frame.index >= dates[0]) & (frame.index <= dates[-1])]
        )
    grid = pd.DatetimeIndex(grid).sort_values()
    _font()
    with plt.rc_context({"font.size": 11, "axes.titlesize": 16, "axes.labelsize": 11}):
        fig, ax = plt.subplots(figsize=(16, 8.5), dpi=100)
        fig.patch.set_facecolor("#f8fafc")
        ax.set_facecolor("#ffffff")
        fig.subplots_adjust(left=0.075, right=0.965, top=0.78, bottom=0.17)
        label = _text(run.get("label", run.get("strategy_id", "策略")))
        # Long or multiline external titles cannot push annotations off the page.
        label = " ".join(label.split())
        fig.text(0.075, 0.935, label[:72], fontsize=20, weight="bold", color="#18243b")
        verified = run.get("validated") is True
        status = (
            "审计检查通过 · 历史回测"
            if verified
            else "模型限制 / 未通过完整验证 · 仅供研究"
        )
        fig.text(
            0.075,
            0.89,
            f"{dates[0]:%Y-%m-%d} — {dates[-1]:%Y-%m-%d}   {status}",
            color="#64748b",
            fontsize=11,
        )
        for interval, color in [(rise, "#daf2df"), (drawdown, "#e5ddf4")]:
            if interval:
                ax.axvspan(
                    dates[interval.start],
                    dates[interval.end],
                    color=color,
                    alpha=0.7,
                    zorder=0,
                )
        fig.text(
            0.075,
            0.842,
            _interval_label("最大上涨", rise, dates),
            color="#267441",
            fontsize=11,
        )
        fig.text(
            0.535,
            0.842,
            _interval_label("最大回撤", drawdown, dates),
            color="#7950a3",
            fontsize=11,
        )
        ax.plot(
            grid,
            pd.Series(nav, index=dates).reindex(grid),
            color="#d94348",
            lw=2,
            label="策略净值",
            zorder=4,
        )
        for key, name, color in [
            ("sse", "上证指数", "#3478e5"),
            ("csi300", "沪深300", "#20a472"),
        ]:
            if key not in benchmarks:
                notes.append(f"{name}：未提供真实行情，未绘制")
                continue
            series = normalize_benchmark(benchmarks[key], grid)
            if series.isna().all():
                notes.append(f"{name}：回测起点缺少有效收盘价，未绘制")
            elif series.isna().any():
                notes.append(f"{name}：{int(series.isna().sum())} 个日期缺失，保留断线")
            ax.plot(grid, series, color=color, lw=1.5, label=name, alpha=0.9)
        for interval, color in [(rise, "#2a8550"), (drawdown, "#8657ad")]:
            if interval:
                positions = [interval.start, interval.end]
                ax.scatter(
                    dates[positions],
                    nav[positions],
                    color=color,
                    s=35,
                    edgecolor="white",
                    linewidth=1,
                    zorder=5,
                )
        ax.axhline(1, color="#cad2dd", lw=0.8, linestyle="--")
        ax.set_ylabel("累计净值（初始资金 = 1）")
        locator = mdates.AutoDateLocator(minticks=5, maxticks=10)
        ax.xaxis.set_major_locator(locator)
        ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
        ax.grid(axis="y", color="#e6ebf1", lw=0.7)
        ax.spines[["top", "right"]].set_visible(False)
        ax.spines[["bottom", "left"]].set_color("#d5dde7")
        ax.tick_params(colors="#64748b")
        ax.legend(
            loc="upper left",
            frameon=True,
            facecolor="white",
            edgecolor="#e2e8f0",
            ncol=3,
        )
        ax.margins(x=0.015, y=0.12)
        fig.text(
            0.075,
            0.075,
            "上证指数、沪深300均为价格指数，不含分红；按回测起点收盘价归一，缺失行情不前填。",
            fontsize=10,
            color="#64748b",
        )
        fig.text(
            0.075,
            0.043,
            "浅绿：先低后高的最大上涨区间    浅紫：先峰后谷的最大回撤区间    区间按已记录净值计算。",
            fontsize=10,
            color="#64748b",
        )
        fig.savefig(output, dpi=100, facecolor=fig.get_facecolor())
        plt.close(fig)
    return notes


def render_matrix(matrix_path: Path, output_dir: Path) -> Path:
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    output_dir.mkdir(parents=True, exist_ok=True)
    dataset = matrix.get("dataset", {})
    benchmarks, benchmark_notes = {}, []
    for key, value in matrix.get("benchmark_files", {}).items():
        try:
            benchmarks[key] = _dated_frame(_resolve(value), ["close"])
        except (OSError, ValueError, TypeError, KeyError) as exc:
            benchmark_notes.append(f"基准 {key} 读取失败：{exc}")
    rows, cards = [], []
    for index, run in enumerate(matrix.get("runs", []), 1):
        chart = f"run-{index:03d}.png"
        label = escape(_text(run.get("label", run.get("strategy_id", f"运行 {index}"))))
        limitations = list(run.get("limitations", [])) + benchmark_notes
        failed = run.get("status") in {"failed", "error"} or bool(run.get("error"))
        try:
            if failed:
                raise ValueError(_text(run.get("error", "运行记录标记失败")))
            limitations.extend(render_run(run, benchmarks, output_dir / chart))
            image = f'<img src="{chart}" alt="{label}：策略与市场基准净值比较" loading="lazy">'
        except (OSError, ValueError, TypeError, KeyError) as exc:
            failed = True
            limitations.append(f"未生成图表：{exc}")
            image = '<div class="empty">运行失败或结果不完整，未生成净值图。</div>'
            # Rerendering a failed matrix must not leave an old successful image.
            (output_dir / chart).unlink(missing_ok=True)
        validated = run.get("validated") is True and not failed
        status = (
            "运行失败 / 结果不完整"
            if failed
            else ("审计检查通过" if validated else "模型限制 / 未通过完整验证")
        )
        badge = f'<span class="badge {"ok" if validated else "warn"}">{status}</span>'
        metrics = run.get("metrics", {}) if not failed else {}
        values = [
            _metric(metrics.get(key), pct)
            for key, pct in [
                ("total_return", True),
                ("annual_return", True),
                ("sharpe", False),
                ("max_drawdown", True),
                ("trade_count", False),
            ]
        ]
        cost = escape(_text(run.get("cost_scenario", "未说明")))
        period = escape(_text(run.get("period", "未说明")))
        rows.append(
            f'<tr><td><a href="#run-{index}">{label}</a>{badge}</td><td>{period}</td><td>{cost}</td>'
            + "".join(f"<td>{v}</td>" for v in values)
            + "</tr>"
        )
        notes = "".join(f"<li>{escape(_text(item))}</li>" for item in limitations)
        cards.append(
            f'<section id="run-{index}"><div class="card-head"><h2>{label}</h2>{badge}</div><p class="muted">策略 {escape(_text(run.get("strategy_id", "—")))} · 区间 {period} · 费用场景 {cost}</p>{image}'
            + (f'<ul class="limitations">{notes}</ul>' if notes else "")
            + "</section>"
        )
    metadata = "".join(
        f"<div><span>{title}</span><strong>{escape(_text(dataset.get(key, '未说明')))}</strong></div>"
        for key, title in [
            ("dataset_id", "数据集"),
            ("universe_count", "样本股票数"),
            ("start_date", "样本起点"),
            ("end_date", "样本终点"),
        ]
    )
    dataset_notes = "".join(
        f"<li>{escape(_text(note))}</li>" for note in dataset.get("limitations", [])
    )
    document = """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>A股策略研究 · 回测报告</title><style>
:root{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;color:#18243b;background:#f3f6fa;font-size:15px}*{box-sizing:border-box}body{margin:0}main{max-width:1500px;margin:auto;padding:48px 32px 64px}header{margin-bottom:28px}.eyebrow{color:#b53b43;letter-spacing:3px;font-size:12px;font-weight:700}h1{font-size:34px;margin:12px 0}h2{font-size:20px;margin:0}.muted{color:#64748b;line-height:1.8}.notice{padding:16px 20px;background:#fff4df;border-left:4px solid #d79527;border-radius:6px;color:#71531f;line-height:1.7}.meta{display:grid;grid-template-columns:2fr 1fr 1fr 1fr;gap:20px;margin:25px 0}.meta span{display:block;color:#64748b;font-size:12px;margin-bottom:8px}.meta strong{font-size:17px;overflow-wrap:anywhere}.hash{font-family:ui-monospace,monospace;font-size:12px;overflow-wrap:anywhere;color:#64748b}section,.table-wrap{margin-top:24px;padding:24px;background:white;border:1px solid #e1e7ef;border-radius:12px}.table-wrap{overflow-x:auto}table{width:100%;border-collapse:collapse;white-space:nowrap}th{text-align:left;color:#64748b;font-weight:500;font-size:12px}td,th{padding:15px 12px;border-bottom:1px solid #edf0f5}td{font-variant-numeric:tabular-nums}td:first-child{white-space:normal;min-width:200px}a{color:#244f87;text-decoration:none}.badge{display:inline-block;font-size:11px;padding:5px 8px;border-radius:5px;font-weight:600;margin:4px 0}.ok{color:#247046;background:#e9f5ed}.warn{color:#945c16;background:#fff0d5}td .badge{display:block;width:max-content}.card-head{display:flex;align-items:center;justify-content:space-between;gap:12px}img{width:100%;height:auto;display:block;border-radius:8px}.limitations{color:#88612b;font-size:13px;line-height:1.8;padding-left:22px}.empty{padding:48px;text-align:center;background:#fff7ed;color:#945c16;border-radius:8px}footer{margin-top:32px;color:#64748b;font-size:12px;line-height:1.8}@media(max-width:700px){main{padding:25px 14px}.meta{grid-template-columns:1fr 1fr}section{padding:14px}.card-head{display:block}h1{font-size:27px}}
</style><main><header><div class="eyebrow">A-SHARE QUANTITATIVE RESEARCH</div><h1>A股策略研究 · 回测报告</h1><p class="muted">多轮历史实验、市场基准与模型审计</p></header><div class="notice">本报告呈现历史回测结果。审计检查通过仅表示已执行检查通过，不代表模型无缺陷、策略当前有效或未来收益。未通过完整验证的运行须结合所列模型限制解读。</div>"""
    document += f'<div class="meta">{metadata}</div><p class="hash">数据内容哈希：{escape(_text(dataset.get("content_hash", "未提供")))}</p>'
    if dataset_notes:
        document += f'<ul class="limitations">{dataset_notes}</ul>'
    research_notes = []
    for note in matrix.get("research_notes", []):
        title = escape(_text(note.get("title", "研究来源")))
        url = _text(note.get("url", ""))
        try:
            parsed = urlsplit(url)
            is_web_url = parsed.scheme in {"https", "http"} and bool(parsed.netloc)
        except ValueError:
            is_web_url = False
        if is_web_url:
            title = f'<a href="{escape(url, quote=True)}" target="_blank" rel="noopener noreferrer">{title}</a>'
        research_notes.append(
            f"<li>{title}：{escape(_text(note.get('text', '')))}</li>"
        )
    if research_notes:
        document += (
            '<section><h2>研究来源与方法说明</h2><ul class="muted">'
            + "".join(research_notes)
            + "</ul></section>"
        )
    document += (
        '<div class="table-wrap"><table><thead><tr><th>实验 / 验证状态</th><th>区间</th><th>费用场景</th><th>总收益</th><th>年化收益</th><th>夏普</th><th>最大回撤</th><th>交易数</th></tr></thead><tbody>'
        + "".join(rows)
        + "</tbody></table></div>"
    )
    document += (
        "".join(cards)
        + "<footer>策略净值 = 每日总资产 / 初始资金。上证指数与沪深300为不含分红的价格指数；基准起点必须有真实收盘价，缺失数据保留断线。绿色与紫色区间分别按时间先后搜索最大上涨和最大回撤；表格指标沿用运行记录，不从图片反推。PNG 与本 HTML 保存在同一目录，可整体复制后本地打开。</footer></main></html>"
    )
    target = output_dir / "index.html"
    target.write_text(document, encoding="utf-8")
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    print(render_matrix(args.matrix.resolve(), args.output_dir.resolve()))


if __name__ == "__main__":
    main()
