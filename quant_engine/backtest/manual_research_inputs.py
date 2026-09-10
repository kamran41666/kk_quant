"""Strict adapters from normalized research tables into manual-daily inputs."""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
from typing import Any

import numpy as np
import pandas as pd

from quant_engine.backtest.manual_research_ledger import ResearchCorporateAction


def _date_or_none(value: Any, field: str) -> date | None:
    if value is None or pd.isna(value):
        return None
    try:
        return pd.Timestamp(value).date()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"corporate_action_{field}_invalid") from exc


def _decimal(value: Any, field: str) -> Decimal:
    if value is None or pd.isna(value):
        raise ValueError(f"corporate_action_{field}_unresolved")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"corporate_action_{field}_invalid") from exc
    if not result.is_finite() or result < 0:
        raise ValueError(f"corporate_action_{field}_invalid")
    return result


def _hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def load_research_corporate_actions(
    frame: pd.DataFrame,
    *,
    source_hash: str,
) -> list[ResearchCorporateAction]:
    required = {
        "code", "record_date", "ex_date", "pay_date", "stock_date",
        "cash_ps", "bonus_ratio", "bonus_allocation_verified",
    }
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"corporate_action_fields_missing:{','.join(sorted(missing))}")
    if len(source_hash) != 64 or any(char not in "0123456789abcdef" for char in source_hash):
        raise ValueError("corporate_action_source_hash_invalid")
    actions = []
    for raw in frame.to_dict("records"):
        code = str(raw["code"]).strip().upper()
        if len(code) != 9 or not code[:6].isdigit() or code[6:] not in {".SH", ".SZ"}:
            raise ValueError("corporate_action_code_invalid")
        verified = raw["bonus_allocation_verified"]
        if not isinstance(verified, (bool, np.bool_)):
            raise ValueError("corporate_action_bonus_allocation_verified_invalid")
        payload = {
            "code": code,
            "record_date": _date_or_none(raw["record_date"], "record_date"),
            "ex_date": _date_or_none(raw["ex_date"], "ex_date"),
            "pay_date": _date_or_none(raw["pay_date"], "pay_date"),
            "stock_listing_date": _date_or_none(raw["stock_date"], "stock_date"),
            "cash_per_share": _decimal(raw["cash_ps"], "cash_ps"),
            "bonus_ratio": _decimal(raw["bonus_ratio"], "bonus_ratio"),
            "allocation_verified": bool(verified),
            "source_hash": source_hash,
        }
        if payload["cash_per_share"] == 0 and payload["bonus_ratio"] == 0:
            continue
        action_id = str(raw.get("action_id") or f"action-{_hash(payload)[:24]}")
        actions.append(ResearchCorporateAction(action_id=action_id, **payload))
    return sorted(actions, key=lambda item: (
        item.record_date or date.max, item.ex_date or date.max, item.code,
        item.economic_key, item.action_id,
    ))


__all__ = ["load_research_corporate_actions"]
