"""Archive and supplement explicitly reported Sina corporate actions, fail closed.

Only unresolved events from 2015 onward and incomplete existing actions are
queried. No action amounts or payment dates are inferred from market prices.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import time
from datetime import UTC, date, datetime
from io import StringIO
from pathlib import Path
from urllib.parse import urlencode

import pandas as pd
import requests

LIST_BASE = (
    "https://vip.stock.finance.sina.com.cn/corp/go.php/vISSUE_ShareBonus/stockid/"
)
DETAIL_BASE = (
    "https://vip.stock.finance.sina.com.cn/corp/view/vISSUE_ShareBonusDetail.php"
)
SINCE = "2015-01-01"


def canonical_code(value: str) -> str:
    value = str(value).upper()
    if re.fullmatch(r"(?:SH|SZ)\.\d{6}", value):
        market, number = value.split(".")
        return f"{number}.{market}"
    if re.fullmatch(r"\d{6}\.(?:SH|SZ)", value):
        return value
    raise ValueError(f"unsupported stock code: {value}")


def _label(value: object) -> str:
    return re.sub(r"[\s（）()、，,:：]", "", str(value))


def _missing(value: object) -> bool:
    return (
        value is None
        or pd.isna(value)
        or str(value).strip() in {"", "--", "—", "-", "nan", "NaT"}
    )


def _number(value: object, field: str, optional: bool = False) -> float | None:
    if _missing(value):
        if optional:
            return None
        raise ValueError(f"missing {field}")
    result = float(str(value).replace(",", "").strip())
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"invalid {field}: {value}")
    return result


def _date(value: object, field: str, required: bool = True) -> str | None:
    if _missing(value):
        if not required:
            return None
        raise ValueError(f"missing {field}")
    result = str(value).strip().replace("/", "-")[:10]
    return date.fromisoformat(result).isoformat()


def parse_dividend_list(html: str) -> list[dict]:
    """Find the dividend table by column meaning, independently of table order."""
    for table in pd.read_html(StringIO(html)):
        columns = {}
        for column in table.columns:
            parts = column if isinstance(column, tuple) else (column,)
            name = _label(
                next(
                    (
                        part
                        for part in reversed(parts)
                        if not str(part).startswith("Unnamed")
                    ),
                    "",
                )
            )
            columns[name] = column
        if not {"公告日期", "除权除息日", "股权登记日", "进度"} <= columns.keys():
            continue
        aliases = {
            "cash10": ["派息税前元", "派息"],
            "bonus10": ["送股股", "送股"],
            "transfer10": ["转增股", "转增"],
        }
        amount_columns = {
            key: next((columns[name] for name in names if name in columns), None)
            for key, names in aliases.items()
        }
        if any(value is None for value in amount_columns.values()):
            continue
        result = []
        for _, row in table.iterrows():
            if str(row[columns["进度"]]).strip() not in {"实施", "实施分配"}:
                continue
            if _missing(row[columns["除权除息日"]]):
                continue
            result.append(
                {
                    "announcement_date": _date(
                        row[columns["公告日期"]], "announcement_date"
                    ),
                    "ex_date": _date(row[columns["除权除息日"]], "ex_date"),
                    "record_date": _date(row[columns["股权登记日"]], "record_date"),
                    **{key: row[column] for key, column in amount_columns.items()},
                }
            )
        return result
    raise ValueError("Sina dividend table with required semantic columns not found")


def parse_detail(
    html: str, listing: dict, code: str, source_url: str, source_sha256: str
) -> dict:
    fields = None
    for table in pd.read_html(StringIO(html)):
        if len(table.columns) != 2:
            continue
        values = {_label(row.iloc[0]): row.iloc[1] for _, row in table.iterrows()}
        if "税前红利报价币种" in values and "登记日" in values and "除息日" in values:
            fields = values
            break
    if fields is None:
        raise ValueError("Sina dividend detail semantic fields not found")
    for name in (
        "配股比例10配",
        "配股价",
        "实际配股比例",
        "实际配股数",
        "转配比例",
        "每股拆细数",
    ):
        if (_number(fields.get(name), name, optional=True) or 0) > 0:
            raise ValueError(
                "rights issue or split present; requires separate verified accounting"
            )
    cash10 = _number(listing.get("cash10"), "listed cash10")
    bonus10 = _number(listing.get("bonus10"), "listed bonus10")
    transfer10 = _number(listing.get("transfer10"), "listed transfer10")
    detail_cash = _number(
        fields.get("税前红利报价币种"), "detail cash10", optional=True
    )
    detail_bonus = _number(fields.get("送股比例10送"), "detail bonus10", optional=True)
    detail_transfer = _number(
        fields.get("转增比例10转增"), "detail transfer10", optional=True
    )
    if detail_transfer is None:
        components = [
            _number(fields.get(key), key, optional=True)
            for key in ("盈余公积金转增比例10转增", "资本公积金转增比例10转增")
        ]
        if any(value is not None for value in components):
            detail_transfer = sum(value or 0 for value in components)
    for name, listed, detailed in [
        ("cash", cash10, detail_cash),
        ("bonus", bonus10, detail_bonus),
        ("transfer", transfer10, detail_transfer),
    ]:
        if detailed is None and listed > 0:
            raise ValueError(f"missing positive {name} in detail")
        if detailed is not None and not math.isclose(
            listed, detailed, rel_tol=1e-8, abs_tol=1e-8
        ):
            raise ValueError(f"list/detail {name} conflict: {listed} != {detailed}")
    cash, bonus = cash10 / 10, (bonus10 + transfer10) / 10
    if cash == 0 and bonus == 0:
        raise ValueError("no explicit cash or bonus distribution")
    record = _date(fields.get("登记日"), "record_date")
    ex = _date(fields.get("除息日"), "ex_date")
    # This source field is explicit; never substitute the ex-date for payment.
    pay = _date(
        fields.get("红利/配股起始日送转股到账日"), "pay_date", required=cash > 0
    )
    stock = _date(fields.get("上市日"), "stock_date", required=bonus > 0)
    if ex != listing["ex_date"] or record != listing["record_date"]:
        raise ValueError("list/detail record or ex-date conflict")
    if record >= ex or (pay and pay < ex) or (stock and stock < ex):
        raise ValueError("corporate-action dates violate chronological order")
    return {
        "code": canonical_code(code),
        "record_date": record,
        "ex_date": ex,
        "pay_date": pay,
        "stock_date": stock,
        "cash_ps": cash,
        "bonus_ratio": bonus,
        "source_url": source_url,
        "source_sha256": source_sha256,
    }


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    os.replace(temporary, path)


class HtmlArchive:
    """One sequential session, bounded timeout/retry, hash-checked raw receipts."""

    def __init__(self, root: Path, session=None, delay: float = 0.4):
        self.root = root
        self.session = session or requests.Session()
        self.delay = delay

    def get(self, code: str, name: str, url: str) -> tuple[str, dict]:
        path = self.root / canonical_code(code) / f"{name}.html"
        metadata_path = path.with_suffix(".json")
        if path.exists() and metadata_path.exists():
            raw = path.read_bytes()
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if (
                metadata["sha256"] != hashlib.sha256(raw).hexdigest()
                or metadata["url"] != url
            ):
                raise ValueError(f"cached response identity mismatch: {path}")
            return raw.decode(metadata["encoding"], errors="replace"), metadata
        if path.exists() or metadata_path.exists():
            raise ValueError(
                f"incomplete cached receipt; review before retrying: {path}"
            )
        response = None
        for attempt in range(2):
            time.sleep(self.delay if attempt == 0 else max(self.delay, 1.0))
            try:
                response = self.session.get(
                    url, timeout=15, headers={"User-Agent": "Mozilla/5.0"}
                )
                response.raise_for_status()
                break
            except requests.RequestException:
                if attempt == 1:
                    raise
        raw = response.content
        encoding = response.apparent_encoding or "gb18030"
        metadata = {
            "url": url,
            "response_url": response.url,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "received_at": datetime.now(UTC).isoformat(),
            "encoding": encoding,
            "status_code": response.status_code,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".html.tmp")
        temporary.write_bytes(raw)
        os.replace(temporary, path)
        _write_json(metadata_path, metadata)
        return raw.decode(encoding, errors="replace"), metadata


def collect_candidates(
    manifest: dict, existing: pd.DataFrame | None = None
) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for security in manifest.get("quality", []):
        code = canonical_code(security["code"])
        for item in security.get("unexplained_reference_adjustments", []):
            day = _date(item["date"], "candidate_date")
            if day >= SINCE and item.get("action_present") is False:
                result.setdefault(code, set()).add(day)
    if existing is not None:
        for row in existing.to_dict("records"):
            day = _date(row.get("ex_date"), "ex_date")
            if day >= SINCE and (
                _missing(row.get("record_date"))
                or (float(row.get("cash_ps", 0)) > 0 and _missing(row.get("pay_date")))
                or (
                    float(row.get("bonus_ratio", 0)) > 0
                    and _missing(row.get("stock_date"))
                )
            ):
                result.setdefault(canonical_code(row["code"]), set()).add(day)
    return result


def supplement(
    manifest_path: Path, output: Path, archive: HtmlArchive | None = None
) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    source = manifest.get("files", {}).get("actions", {}).get("path")
    existing = None
    if source:
        path = Path(source)
        if not path.is_absolute():
            path = manifest_path.parent / path
        existing = pd.read_parquet(path)
    candidates = collect_candidates(manifest, existing)
    archive = archive or HtmlArchive(output.parent / "sina_actions")
    existing_map = (
        {
            (canonical_code(row["code"]), str(row["ex_date"])[:10]): row
            for row in existing.to_dict("records")
        }
        if existing is not None
        else {}
    )
    actions, errors = [], []
    for code, dates in sorted(candidates.items()):
        list_url = f"{LIST_BASE}{code[:6]}.phtml"
        try:
            html, _ = archive.get(code, "dividends", list_url)
            listing = parse_dividend_list(html)
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            requests.RequestException,
        ) as exc:
            errors.extend(
                {
                    "code": code,
                    "date": day,
                    "stage": "list",
                    "error": str(exc),
                    "source_url": list_url,
                }
                for day in sorted(dates)
            )
            _write_json(output, {"actions": actions, "errors": errors})
            continue
        for day in sorted(dates):
            url = list_url
            try:
                matched = [row for row in listing if row["ex_date"] == day]
                if len(matched) != 1:
                    raise ValueError(
                        f"expected one implemented dividend, found {len(matched)}; may be rights issue or absent source"
                    )
                row = matched[0]
                url = (
                    DETAIL_BASE
                    + "?"
                    + urlencode(
                        {
                            "stockid": code[:6],
                            "type": "1",
                            "end_date": row["announcement_date"],
                        }
                    )
                )
                detail, metadata = archive.get(code, row["announcement_date"], url)
                action = parse_detail(detail, row, code, url, metadata["sha256"])
                prior = existing_map.get((code, day))
                if prior:
                    for field in ("cash_ps", "bonus_ratio"):
                        if not math.isclose(
                            float(prior[field]),
                            action[field],
                            rel_tol=1e-8,
                            abs_tol=1e-8,
                        ):
                            raise ValueError(
                                f"existing action {field} conflict; no overwrite"
                            )
                    for field in ("record_date", "pay_date", "stock_date"):
                        if (
                            not _missing(prior.get(field))
                            and _date(prior[field], field) != action[field]
                        ):
                            raise ValueError(
                                f"existing action {field} conflict; no overwrite"
                            )
                actions.append(action)
            except (
                OSError,
                ValueError,
                TypeError,
                KeyError,
                requests.RequestException,
            ) as exc:
                errors.append(
                    {
                        "code": code,
                        "date": day,
                        "stage": "detail",
                        "error": str(exc),
                        "source_url": url,
                    }
                )
            _write_json(output, {"actions": actions, "errors": errors})
        print(
            json.dumps(
                {
                    "code": code,
                    "candidates": len(dates),
                    "actions": len(actions),
                    "errors": len(errors),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    payload = {"actions": actions, "errors": errors}
    _write_json(output, payload)
    return payload


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    result = supplement(arguments.manifest.resolve(), arguments.output.resolve())
    print(
        json.dumps(
            {
                "actions": len(result["actions"]),
                "errors": len(result["errors"]),
                "output": str(arguments.output),
            },
            ensure_ascii=False,
        )
    )
