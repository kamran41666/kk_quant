import json

import numpy as np
import pandas as pd
import pytest

from scripts.render_ashare_research import (
    maximum_drawdown,
    maximum_rise,
    normalize_benchmark,
    render_matrix,
)


def test_extrema_respect_time_order_and_relative_return():
    rise = maximum_rise([10, 5, 9, 2, 3])
    drawdown = maximum_drawdown([10, 5, 9, 2, 3])
    assert (rise.start, rise.end, rise.change) == (1, 2, 0.8)
    assert (drawdown.start, drawdown.end, drawdown.change) == (0, 3, -0.8)


def test_monotonic_and_constant_intervals():
    assert maximum_rise([1, 2, 4]).change == 3
    assert maximum_drawdown([4, 2, 1]).change == -0.75
    assert maximum_rise([4, 2, 1]) is None
    assert maximum_drawdown([1, 2, 4]) is None
    for values in ([1], [2, 2, 2]):
        assert maximum_rise(values) is None
        assert maximum_drawdown(values) is None


@pytest.mark.parametrize(
    "values", [[], [0, 1], [-1, 2], [1, np.nan], [1, np.inf], [[1, 2]]]
)
def test_invalid_values_are_rejected(values):
    for function in (maximum_rise, maximum_drawdown):
        with pytest.raises(ValueError):
            function(values)


def test_benchmark_gaps_are_never_filled_and_baseline_is_exact():
    dates = pd.date_range("2020-01-01", periods=4)
    frame = pd.DataFrame({"close": [10, 12, 0]}, index=dates[[0, 2, 3]])
    result = normalize_benchmark(frame, dates)
    assert result.iloc[0] == 1
    assert np.isnan(result.iloc[1]) and np.isnan(result.iloc[3])
    assert result.iloc[2] == 1.2
    assert normalize_benchmark(frame.iloc[1:], dates).isna().all()


def test_report_escapes_metadata_and_records_failed_runs(tmp_path):
    matrix = {
        "dataset": {"dataset_id": "<script>bad()</script>", "content_hash": "abc"},
        "runs": [
            {
                "label": '<img src=x onerror="bad()">',
                "strategy_id": "s1",
                "validated": True,
                "result_dir": str(tmp_path / "missing"),
                "metrics": {"total_return": 99},
            }
        ],
    }
    source = tmp_path / "matrix.json"
    source.write_text(json.dumps(matrix), encoding="utf-8")
    output = tmp_path / "report"
    output.mkdir()
    (output / "run-001.png").write_bytes(b"stale")
    report = render_matrix(source, output).read_text(encoding="utf-8")
    assert "&lt;script&gt;" in report and "<script>" not in report
    assert "&lt;img src=x" in report
    assert "运行失败 / 结果不完整" in report
    assert "9900.00%" not in report
    assert not (output / "run-001.png").exists()


def test_valid_report_uses_initial_capital_and_relative_image(tmp_path):
    dates = pd.date_range("2020-01-01", periods=3)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    pd.DataFrame(
        {"date": dates, "total_value": [100, 110, 99], "daily_return": [0, 0.1, -0.1]}
    ).to_parquet(run_dir / "daily_portfolio.parquet")
    (run_dir / "summary.json").write_text('{"initial_capital": 100}', encoding="utf-8")
    benchmark = tmp_path / "benchmark.parquet"
    pd.DataFrame({"date": dates, "close": [10, 11, 12]}).to_parquet(benchmark)
    source = tmp_path / "matrix.json"
    source.write_text(
        json.dumps(
            {
                "dataset": {"limitations": ["测试样本"]},
                "benchmark_files": {"sse": str(benchmark), "csi300": str(benchmark)},
                "runs": [
                    {"label": "测试图", "result_dir": str(run_dir), "validated": False}
                ],
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "report"
    html = render_matrix(source, output).read_text(encoding="utf-8")
    assert 'src="run-001.png"' in html
    assert "模型限制 / 未通过完整验证" in html
    assert (output / "run-001.png").stat().st_size > 1000


def test_research_links_are_escaped_and_only_allow_web_urls(tmp_path):
    source = tmp_path / "matrix.json"
    source.write_text(
        json.dumps(
            {
                "research_notes": [
                    {
                        "title": "<来源>",
                        "text": "<公式>",
                        "url": "https://example.com/paper?a=1&b=2",
                    },
                    {"title": "unsafe", "text": "text", "url": "javascript:alert(1)"},
                ]
            }
        ),
        encoding="utf-8",
    )
    html = render_matrix(source, tmp_path / "report").read_text(encoding="utf-8")
    assert 'href="https://example.com/paper?a=1&amp;b=2"' in html
    assert "&lt;来源&gt;" in html and "&lt;公式&gt;" in html
    assert "javascript:" not in html
