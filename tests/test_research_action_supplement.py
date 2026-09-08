import json

import pandas as pd
import pytest
import requests

from scripts.supplement_research_actions import (
    HtmlArchive,
    collect_candidates,
    parse_detail,
    parse_dividend_list,
    supplement,
)


def table(rows):
    return pd.DataFrame(rows).to_html(index=False, header=False)


def listing_html():
    return table([["unrelated", 1]]) + pd.DataFrame(
        [
            {
                "公告日期": "2024-12-20",
                "派息(税前)(元)": 1.2,
                "送股(股)": 0,
                "转增(股)": 0,
                "股权登记日": "2024-12-26",
                "除权除息日": "2024-12-27",
                "进度": "实施",
            },
            {
                "公告日期": "2025-01-01",
                "派息(税前)(元)": 2,
                "送股(股)": 0,
                "转增(股)": 0,
                "股权登记日": "2025-01-02",
                "除权除息日": "2025-01-03",
                "进度": "预案",
            },
        ]
    ).to_html(index=False)


def detail_html(pay="2024-12-27", bonus="--", stock="--", rights="--"):
    return table([["irrelevant", 3]]) + table(
        [
            ["税前红利（报价币种）", "1.20"],
            ["登记日", "2024-12-26"],
            ["除息日", "2024-12-27"],
            ["红利/配股起始日（送、转股到账日）", pay],
            ["送股比例（10送）", bonus],
            ["转增比例（10转增）", "--"],
            ["上市日", stock],
            ["配股比例（10配）", rights],
        ]
    )


def test_semantic_tables_and_per_share_conversion():
    listing = parse_dividend_list(listing_html())
    assert len(listing) == 1
    result = parse_detail(detail_html(), listing[0], "sh.600167", "source", "hash")
    assert result["code"] == "600167.SH"
    assert result["cash_ps"] == 0.12 and result["bonus_ratio"] == 0
    assert result["stock_date"] is None


def test_payment_date_is_explicit_and_can_follow_ex_date():
    result = parse_detail(
        detail_html(pay="2025-01-02"),
        parse_dividend_list(listing_html())[0],
        "600167.SH",
        "url",
        "hash",
    )
    assert result["pay_date"] == "2025-01-02"
    with pytest.raises(ValueError, match="missing pay_date"):
        parse_detail(
            detail_html(pay="--"),
            parse_dividend_list(listing_html())[0],
            "600167.SH",
            "url",
            "hash",
        )


def test_bonus_requires_listing_date_and_no_rights_are_guessed():
    listing = parse_dividend_list(listing_html())[0]
    listing["bonus10"] = 3
    with pytest.raises(ValueError, match="missing stock_date"):
        parse_detail(detail_html(bonus="3"), listing, "600167.SH", "url", "hash")
    action = parse_detail(
        detail_html(bonus="3", stock="2024-12-30"), listing, "600167.SH", "url", "hash"
    )
    assert action["bonus_ratio"] == 0.3 and action["stock_date"] == "2024-12-30"
    with pytest.raises(ValueError, match="rights issue"):
        parse_detail(detail_html(rights="2"), listing, "600167.SH", "url", "hash")


def test_list_detail_conflict_fails_closed():
    listing = parse_dividend_list(listing_html())[0]
    listing["cash10"] = 2
    with pytest.raises(ValueError, match="cash conflict"):
        parse_detail(detail_html(), listing, "600167.SH", "url", "hash")


def test_candidate_scope_and_incomplete_actions():
    manifest = {
        "quality": [
            {
                "code": "sh.600167",
                "unexplained_reference_adjustments": [
                    {"date": "2014-12-01", "action_present": False},
                    {"date": "2024-12-27", "action_present": False},
                    {"date": "2023-07-06", "action_present": True},
                ],
            }
        ]
    }
    assert collect_candidates(manifest) == {"600167.SH": {"2024-12-27"}}
    existing = pd.DataFrame(
        [
            {
                "code": "600167.SH",
                "ex_date": "2019-05-29",
                "record_date": "2019-05-28",
                "cash_ps": 0.15,
                "pay_date": "2019-05-29",
                "bonus_ratio": 0.3,
                "stock_date": None,
            }
        ]
    )
    assert "2019-05-29" in collect_candidates(manifest, existing)["600167.SH"]


def test_archive_has_bounded_retry_timeout_and_detects_mutation(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "scripts.supplement_research_actions.time.sleep", lambda _: None
    )

    class Session:
        calls = 0

        def get(self, url, **kwargs):
            assert kwargs["timeout"] == 15
            self.calls += 1
            if self.calls == 1:
                raise requests.Timeout("transient")
            response = requests.Response()
            response.status_code = 200
            response.url = url
            response._content = b"<html>archived</html>"
            return response

    session = Session()
    archive = HtmlArchive(tmp_path, session=session, delay=0)
    text, metadata = archive.get("600167.SH", "dividends", "https://example.com")
    assert session.calls == 2 and len(metadata["sha256"]) == 64
    assert archive.get("600167.SH", "dividends", "https://example.com")[0] == text
    assert session.calls == 2
    (tmp_path / "600167.SH/dividends.html").write_text("changed")
    with pytest.raises(ValueError, match="identity mismatch"):
        archive.get("600167.SH", "dividends", "https://example.com")


def test_supplement_records_absent_actions_and_preserves_conflicting_existing(tmp_path):
    class Archive:
        def get(self, code, name, url):
            return (listing_html() if name == "dividends" else detail_html()), {
                "sha256": "a" * 64
            }

    actions = tmp_path / "actions.parquet"
    pd.DataFrame(
        [
            {
                "code": "600167.SH",
                "ex_date": "2024-12-27",
                "record_date": "2024-12-26",
                "cash_ps": 0.2,
                "bonus_ratio": 0,
                "pay_date": None,
                "stock_date": None,
            }
        ]
    ).to_parquet(actions)
    manifest = {
        "files": {"actions": {"path": str(actions)}},
        "quality": [
            {
                "code": "600167.SH",
                "unexplained_reference_adjustments": [
                    {"date": "2020-01-02", "action_present": False}
                ],
            }
        ],
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    result = supplement(path, tmp_path / "supplemental_actions.json", archive=Archive())
    assert result["actions"] == [] and len(result["errors"]) == 2
    assert any("conflict" in error["error"] for error in result["errors"])
    assert any("found 0" in error["error"] for error in result["errors"])


def test_archive_stops_after_two_failures_and_saves_no_valid_receipt(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "scripts.supplement_research_actions.time.sleep", lambda _: None
    )

    class Session:
        calls = 0

        def get(self, url, **kwargs):
            self.calls += 1
            raise requests.Timeout("still unavailable")

    session = Session()
    archive = HtmlArchive(tmp_path, session=session, delay=0)
    with pytest.raises(requests.Timeout):
        archive.get("600167.SH", "dividends", "https://example.com")
    assert session.calls == 2
    assert not list(tmp_path.rglob("*.json"))


def test_share_only_distribution_uses_explicit_list_zero_cash():
    listing = parse_dividend_list(listing_html())[0]
    listing["cash10"] = 0
    listing["bonus10"] = 3
    html = detail_html(pay="--", bonus="3", stock="2024-12-30").replace("1.20", "--")
    action = parse_detail(html, listing, "600167.SH", "url", "hash")
    assert action["cash_ps"] == 0
    assert action["pay_date"] is None
    assert action["bonus_ratio"] == 0.3
