"""Independent, immutable bundle contract for the manual daily route.

This is intentionally a new dataclass.  ``FrozenFactorStrategyBundle`` and
its v1 identity remain unchanged so historical research hashes do not move.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
from typing import Any


def _sha(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ManualDailyFactorBundleV2:
    """Research identity for T-close -> T+1-open -> T+2-close execution."""

    strategy_key: str
    factor_name: str
    factor_expression_hash: str
    label_spec_hash: str
    training_evidence_hash: str
    validation_evidence_hash: str
    dataset_content_hash: str
    signal_phase: str = "close"
    entry_offset: int = 1
    entry_phase: str = "open"
    exit_offset: int = 2
    exit_phase: str = "close"
    cohort_count: int = 2
    cohort_gross_exposure: Decimal = Decimal("0.45")
    entry_expiry: int = 1
    exit_retry_policy: str = "next_trading_close"
    top_n: int = 50
    cost_scenario: str = "baseline"
    code_hash: str = ""

    def __post_init__(self) -> None:
        if not str(self.strategy_key).strip() or not str(self.factor_name).strip():
            raise ValueError("strategy_key and factor_name are required")
        for name in (
            "factor_expression_hash", "label_spec_hash", "training_evidence_hash",
            "validation_evidence_hash", "dataset_content_hash",
        ):
            value = str(getattr(self, name))
            if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                raise ValueError(f"{name} must be a SHA-256 hex digest")
        if self.code_hash and (len(self.code_hash) != 64 or any(char not in "0123456789abcdef" for char in self.code_hash)):
            raise ValueError("code_hash must be a SHA-256 hex digest")
        if (self.signal_phase, self.entry_phase, self.exit_phase) != ("close", "open", "close"):
            raise ValueError("manual daily phases must be close, open, close")
        if (self.entry_offset, self.exit_offset) != (1, 2):
            raise ValueError("manual daily offsets must be T+1 and T+2")
        if self.cohort_count != 2 or self.entry_expiry < 1:
            raise ValueError("manual daily v2 requires two cohorts and positive entry expiry")
        exposure = self.cohort_gross_exposure if isinstance(self.cohort_gross_exposure, Decimal) else Decimal(str(self.cohort_gross_exposure))
        if not Decimal("0") < exposure <= Decimal("0.45"):
            raise ValueError("each cohort exposure must be in (0, 0.45]")
        object.__setattr__(self, "cohort_gross_exposure", exposure)
        if self.top_n < 1 or self.cost_scenario not in {"baseline", "stress"}:
            raise ValueError("invalid manual daily portfolio policy")

    def identity(self) -> dict[str, Any]:
        return {
            "protocol_version": "manual-daily-factor-bundle-v2",
            "strategy_key": self.strategy_key,
            "factor_name": self.factor_name,
            "factor_expression_hash": self.factor_expression_hash,
            "label_spec_hash": self.label_spec_hash,
            "training_evidence_hash": self.training_evidence_hash,
            "validation_evidence_hash": self.validation_evidence_hash,
            "dataset_content_hash": self.dataset_content_hash,
            "execution_policy": {
                "signal_phase": self.signal_phase, "entry_offset": self.entry_offset,
                "entry_phase": self.entry_phase, "exit_offset": self.exit_offset,
                "exit_phase": self.exit_phase, "cohort_count": self.cohort_count,
                "cohort_gross_exposure": str(self.cohort_gross_exposure),
                "entry_expiry": self.entry_expiry, "exit_retry_policy": self.exit_retry_policy,
                "auto_submit": False,
            },
            "portfolio_policy": {"top_n": self.top_n, "cost_scenario": self.cost_scenario},
            "code_hash": self.code_hash,
        }

    @property
    def bundle_hash(self) -> str:
        return _sha(self.identity())

    def as_dict(self) -> dict[str, Any]:
        return {**self.identity(), "bundle_hash": self.bundle_hash}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ManualDailyFactorBundleV2":
        raw = dict(payload)
        expected_hash = raw.pop("bundle_hash", None)
        protocol_version = raw.pop("protocol_version", "manual-daily-factor-bundle-v2")
        if protocol_version != "manual-daily-factor-bundle-v2":
            raise ValueError("unsupported manual daily bundle protocol")
        execution = dict(raw.pop("execution_policy", {}))
        portfolio = dict(raw.pop("portfolio_policy", {}))
        if execution.pop("auto_submit", False):
            raise ValueError("manual daily bundle cannot auto submit")
        raw.update(execution)
        raw.update(portfolio)
        bundle = cls(**raw)
        if expected_hash is not None and expected_hash != bundle.bundle_hash:
            raise ValueError("manual daily bundle hash mismatch")
        return bundle


__all__ = ["ManualDailyFactorBundleV2"]
