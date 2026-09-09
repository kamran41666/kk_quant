"""Immutable evidence bundle for a restricted factor portfolio strategy."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from typing import Any, Mapping

from quant_engine.factor.expression import FactorExpressionSpec


@dataclass(frozen=True)
class FrozenFactorStrategyBundle:
    expression: FactorExpressionSpec
    training_experiment_id: str
    validation_experiment_id: str
    training_result_hash: str
    validation_result_hash: str
    training_artifact_hashes: Mapping[str, str]
    validation_artifact_hashes: Mapping[str, str]
    dataset_id: str
    data_content_hash: str
    forward_horizon: int
    evaluation_policy: Mapping[str, Any]
    code_hashes: Mapping[str, str]
    top_n: int
    gross_exposure: float
    rebalance_frequency: str

    def __post_init__(self) -> None:
        if self.expression.role != "rank":
            raise ValueError("only rank expressions can be frozen as standalone portfolios")
        for value in (
            self.training_result_hash,
            self.validation_result_hash,
            self.data_content_hash,
        ):
            if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
                raise ValueError("factor bundle hashes must be SHA-256 hex digests")
        for artifacts in (
            self.training_artifact_hashes,
            self.validation_artifact_hashes,
            self.code_hashes,
        ):
            if not artifacts or any(
                len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value)
                for value in artifacts.values()
            ):
                raise ValueError("factor bundle artifact hashes must be nonempty SHA-256 mappings")
        if not 1 <= self.forward_horizon <= 60:
            raise ValueError("factor bundle forward_horizon must be in [1, 60]")
        if not 5 <= self.top_n <= 100:
            raise ValueError("factor bundle top_n must be in [5, 100]")
        if not math.isfinite(self.gross_exposure) or not 0.1 <= self.gross_exposure <= 0.9:
            raise ValueError("factor bundle gross_exposure must be in [0.1, 0.9]")
        if self.rebalance_frequency not in {"weekly", "monthly"}:
            raise ValueError("factor bundle frequency must be weekly or monthly")
        json.dumps(dict(self.evaluation_policy), sort_keys=True, allow_nan=False)

    def identity(self) -> dict[str, Any]:
        return {
            "protocol_version": "frozen-factor-strategy-v1",
            "expression": self.expression.as_dict(),
            "training_experiment_id": self.training_experiment_id,
            "validation_experiment_id": self.validation_experiment_id,
            "training_result_hash": self.training_result_hash,
            "validation_result_hash": self.validation_result_hash,
            "training_artifact_hashes": dict(self.training_artifact_hashes),
            "validation_artifact_hashes": dict(self.validation_artifact_hashes),
            "dataset_id": self.dataset_id,
            "data_content_hash": self.data_content_hash,
            "forward_horizon": self.forward_horizon,
            "evaluation_policy": dict(self.evaluation_policy),
            "code_hashes": dict(self.code_hashes),
            "portfolio_policy": {
                "top_n": self.top_n,
                "gross_exposure": self.gross_exposure,
                "rebalance_frequency": self.rebalance_frequency,
            },
        }

    @property
    def bundle_hash(self) -> str:
        payload = json.dumps(
            self.identity(), sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        return {**self.identity(), "bundle_hash": self.bundle_hash}
