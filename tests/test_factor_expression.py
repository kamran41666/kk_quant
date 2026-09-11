"""Restricted factor expressions and persistent experiment workflow."""
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from quant_engine.factor.expression import FactorExpressionError, FactorExpressionSpec
from quant_engine.factor.frozen import FrozenFactorStrategyBundle
from quant_engine.factor.gates import FactorGateError, FactorGatePolicy, apply_training_gates
from quant_engine.factor.generation import TemplateCandidateGenerator
from quant_engine.factor.strategy import build_expression_rank_strategy
from server.models.database import Base
from server.services.factor_research import (
    claim_next_experiment,
    execute_experiment,
    generate_candidates,
    queue_experiment,
    register_candidate,
    research_memory,
    retry_experiment,
    serialize_experiment,
)


def expression_payload(name="price_acceleration"):
    return {
        "name": name,
        "hypothesis": "滞后收益经过短窗平滑后刻画价格持续性。",
        "direction": 1,
        "role": "rank",
        "source": "test",
        "expression": {
            "op": "winsorize_zscore",
            "args": [{
                "op": "ts_mean",
                "args": [{
                    "op": "div",
                    "args": [
                        {
                            "op": "sub",
                            "args": [
                                {"field": "close"},
                                {"op": "delay", "args": [{"field": "close"}], "params": {"periods": 1}},
                            ],
                        },
                        {"op": "delay", "args": [{"field": "close"}], "params": {"periods": 1}},
                    ],
                }],
                "params": {"window": 3, "min_periods": 2},
            }],
            "params": {"lower": 0.01, "upper": 0.99},
        },
    }


def test_expression_is_canonical_and_lookback_is_recursive():
    first = FactorExpressionSpec.from_dict(expression_payload())
    second_payload = expression_payload("renamed_factor")
    second_payload["hypothesis"] = "A different description of the same expression."
    second = FactorExpressionSpec.from_dict(second_payload)
    assert first.expression_hash == second.expression_hash
    assert first.required_fields == ("close",)
    assert first.lookback == 4


@pytest.mark.parametrize(
    "mutation, message",
    [
        (lambda value: value["expression"].update(op="eval"), "not allowed"),
        (
            lambda value: value["expression"]["args"][0]["args"][0]["args"][1]["params"].update(periods=-1),
            "invalid periods",
        ),
        (lambda value: value.update(direction=0), "direction"),
        (lambda value: value.update(expression={"constant": float("inf")}), "non-finite"),
    ],
)
def test_expression_rejects_unsafe_or_invalid_payloads(mutation, message):
    payload = expression_payload()
    mutation(payload)
    with pytest.raises(FactorExpressionError, match=message):
        FactorExpressionSpec.from_dict(payload)


def test_expression_computes_aligned_finite_cross_sections():
    spec = FactorExpressionSpec.from_dict(expression_payload())
    dates = pd.date_range("2024-01-02", periods=8, freq="B").date
    codes = ["A", "B", "C"]
    index = pd.MultiIndex.from_product([codes, dates], names=["code", "date"])
    close = np.concatenate([
        np.linspace(10, 13, len(dates)),
        np.linspace(20, 19, len(dates)),
        np.linspace(30, 36, len(dates)),
    ])
    frame = pd.DataFrame({"close": close}, index=index)
    result = spec.compute(frame)
    assert result.index.equals(frame.index)
    last = result.xs(dates[-1], level="date")
    assert np.isfinite(last).all()
    assert last.mean() == pytest.approx(0.0, abs=1e-12)


def test_training_gate_policy_reports_each_failed_reason():
    policy = FactorGatePolicy.from_dict()
    decision, gates = apply_training_gates(
        policy,
        coverage=0.8,
        ic_observations=100,
        directional_ic=-0.01,
        maximum_absolute_correlation=0.9,
        half_sign_consistent=False,
        directional_quantile_spread=-0.001,
    )
    assert decision == "training_rejected"
    assert {item["name"] for item in gates if not item["passed"]} == {
        "directional_ic",
        "baseline_correlation",
        "half_sign_consistency",
        "directional_quantile_spread",
    }


def test_training_gate_policy_rejects_unknown_or_nonfinite_thresholds():
    with pytest.raises(FactorGateError, match="unknown"):
        FactorGatePolicy.from_dict({"magic_threshold": 1})
    with pytest.raises(FactorGateError, match="finite"):
        FactorGatePolicy.from_dict({"min_directional_ic": float("nan")})


def test_template_generator_uses_expression_hash_memory_and_limit():
    generator = TemplateCandidateGenerator()
    first = generator.generate(memory={"items": []}, limit=2)
    assert len(first) == 2
    second = generator.generate(
        memory={"items": [{"expression_hash": first[0].expression_hash}]},
        limit=4,
    )
    assert all(item.expression_hash != first[0].expression_hash for item in second)
    assert len({item.expression_hash for item in second}) == len(second)


def test_frozen_strategy_bundle_hash_covers_evidence_and_portfolio_policy():
    expression = FactorExpressionSpec.from_dict(expression_payload())
    values = dict(
        expression=expression,
        training_experiment_id="training-id",
        validation_experiment_id="validation-id",
        training_result_hash="1" * 64,
        validation_result_hash="2" * 64,
        training_artifact_hashes={"report.json": "3" * 64},
        validation_artifact_hashes={"report.json": "4" * 64},
        dataset_id="dataset-v1",
        data_content_hash="5" * 64,
        forward_horizon=5,
        evaluation_policy=FactorGatePolicy().as_dict(),
        code_hashes={"engine.py": "6" * 64},
        top_n=50,
        gross_exposure=0.9,
        rebalance_frequency="weekly",
    )
    first = FrozenFactorStrategyBundle(**values)
    second = FrozenFactorStrategyBundle(**values)
    changed = FrozenFactorStrategyBundle(**{**values, "top_n": 100})
    assert first.bundle_hash == second.bundle_hash
    assert first.bundle_hash != changed.bundle_hash
    assert build_expression_rank_strategy(first.expression, bundle_hash=first.bundle_hash).SPEC.extensions[
        "factor_strategy_bundle_hash"
    ] == first.bundle_hash


def test_filter_expression_cannot_become_standalone_rank_strategy():
    payload = expression_payload("filter_only")
    payload["role"] = "filter"
    with pytest.raises(ValueError, match="only rank"):
        build_expression_rank_strategy(FactorExpressionSpec.from_dict(payload))


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def frozen_dataset(root: Path) -> str:
    target = root / "research" / "test-factor-v1"
    target.mkdir(parents=True)
    dates = pd.bdate_range("2024-01-02", periods=14).date
    codes = ["600000.SH", "000001.SZ", "600006.SH", "000002.SZ"]
    rows = []
    for code_index, code in enumerate(codes):
        for day_index, day in enumerate(dates):
            price = 10.0 + code_index * 3 + day_index * (0.1 + code_index * 0.03)
            rows.append({
                "code": code,
                "date": day,
                "open": price,
                "high": price * 1.01,
                "low": price * 0.99,
                "close": price * (1 + (code_index - 1.5) * 0.001),
                "volume": 1_000_000.0 + day_index * 1000,
                "amount": price * 1_000_000.0,
                "turnover_rate": 0.01 + code_index * 0.001,
                "is_suspended": False,
                "is_st": False,
                "adjusted_close": price * (1 + (code_index - 1.5) * 0.001),
            })
    daily_path = target / "daily.parquet"
    security_path = target / "securities.parquet"
    pd.DataFrame(rows).to_parquet(daily_path, index=False)
    pd.DataFrame([
        {"code": code, "ipo_date": "2000-01-01", "out_date": None}
        for code in codes
    ]).to_parquet(security_path, index=False)
    content_hash = "a" * 64
    manifest = {
        "dataset_id": "test-factor-v1",
        "content_hash": content_hash,
        "start_date": dates[0].isoformat(),
        "end_date": dates[-1].isoformat(),
        "files": {
            "daily": {"path": str(daily_path), "sha256": _sha(daily_path)},
            "securities": {"path": str(security_path), "sha256": _sha(security_path)},
        },
    }
    (target / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return content_hash


def test_candidate_queue_is_idempotent_and_worker_persists_artifacts(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False)
    content_hash = frozen_dataset(tmp_path / "data")
    with factory() as db:
        candidate, created = register_candidate(db, expression_payload())
        duplicate, duplicate_created = register_candidate(
            db, expression_payload("same_expression_new_name")
        )
        assert created is True
        assert duplicate_created is False
        assert duplicate.id == candidate.id
        conflicting = expression_payload("same_expression_opposite_direction")
        conflicting["direction"] = -1
        with pytest.raises(ValueError, match="different direction or role"):
            register_candidate(db, conflicting)

        first, queued = queue_experiment(
            db,
            candidate_id=candidate.id,
            dataset_id="test-factor-v1",
            start=date(2024, 1, 4),
            end=date(2024, 1, 17),
            forward_horizon=2,
            data_root=tmp_path / "data",
        )
        duplicate_run, duplicate_queued = queue_experiment(
            db,
            candidate_id=candidate.id,
            dataset_id="test-factor-v1",
            start=date(2024, 1, 4),
            end=date(2024, 1, 17),
            forward_horizon=2,
            data_root=tmp_path / "data",
        )
        assert queued is True
        assert duplicate_queued is False
        assert duplicate_run.id == first.id
        assert first.data_content_hash == content_hash
        with pytest.raises(ValueError, match="different gate policy"):
            queue_experiment(
                db,
                candidate_id=candidate.id,
                dataset_id="test-factor-v1",
                start=date(2024, 1, 4),
                end=date(2024, 1, 17),
                forward_horizon=2,
                evaluation_policy={"min_coverage": 0.8},
                data_root=tmp_path / "data",
            )

        claimed = claim_next_experiment(db, worker_id="test-worker", lease_seconds=30)
        assert claimed.id == first.id
        assert claimed.attempt == 1
        report = execute_experiment(
            db,
            claimed.id,
            worker_id="test-worker",
            data_root=tmp_path / "data",
            result_root=tmp_path / "results",
        )
        state = serialize_experiment(db.get(type(first), first.id))
        assert state["status"] == "completed"
        assert state["result"]["expression_hash"] == candidate.expression_hash
        assert report["finite_count"] > 0
        assert report["decision"] == "training_rejected"
        assert report["gate_results"]
        artifact = Path(state["artifact_dir"])
        assert (artifact / "factor_values.parquet").exists()
        assert (artifact / "ic_series.parquet").exists()
        assert (artifact / "quantile_returns.parquet").exists()
        assert json.loads((artifact / "report.json").read_text())["data_content_hash"] == content_hash
        memory = research_memory(db)
        assert memory["counts"]["training_rejected"] == 1
        assert memory["items"][0]["failed_gates"]


def test_expired_worker_lease_can_be_reclaimed(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False)
    frozen_dataset(tmp_path / "data")
    with factory() as db:
        candidate, _ = register_candidate(db, expression_payload())
        experiment, _ = queue_experiment(
            db,
            candidate_id=candidate.id,
            dataset_id="test-factor-v1",
            start=date(2024, 1, 8),
            end=date(2024, 1, 10),
            data_root=tmp_path / "data",
        )
        experiment.status = "running"
        experiment.lease_owner = "dead-worker"
        experiment.lease_until = (date.today() - timedelta(days=1)).isoformat()
        db.commit()
        reclaimed = claim_next_experiment(db, worker_id="replacement", lease_seconds=30)
        assert reclaimed.id == experiment.id
        assert reclaimed.lease_owner == "replacement"
        assert reclaimed.attempt == 1


def test_failed_experiment_can_be_requeued_without_losing_attempt_history(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False)
    frozen_dataset(tmp_path / "data")
    with factory() as db:
        candidate, _ = register_candidate(db, expression_payload())
        experiment, _ = queue_experiment(
            db,
            candidate_id=candidate.id,
            dataset_id="test-factor-v1",
            start=date(2024, 1, 2),
            end=date(2024, 1, 10),
            data_root=tmp_path / "data",
        )
        experiment.status = "failed"
        experiment.attempt = 2
        experiment.error_code = "SyntheticFailure"
        experiment.error_message = "failure detail"
        db.commit()
        retried = retry_experiment(db, experiment.id, data_root=tmp_path / "data")
        assert retried.status == "queued"
        assert retried.attempt == 2
        assert retried.error_code is None
        claimed = claim_next_experiment(db, worker_id="retry-worker", lease_seconds=30)
        assert claimed.id == experiment.id
        assert claimed.attempt == 3


def test_generation_registers_new_templates_once_and_memory_excludes_validation(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False)
    frozen_dataset(tmp_path / "data")
    with factory() as db:
        generated = generate_candidates(db, limit=3)
        assert generated["generated"] == 3
        assert all(item["created"] for item in generated["candidates"])
        assert generate_candidates(db, limit=4)["generated"] == 1
        assert generate_candidates(db, limit=4)["generated"] == 0

        candidate_id = generated["candidates"][0]["candidate"]["id"]
        with pytest.raises(ValueError, match="requires earlier, matching training_passed"):
            queue_experiment(
                db,
                candidate_id=candidate_id,
                dataset_id="test-factor-v1",
                start=date(2024, 1, 6),
                end=date(2024, 1, 10),
                stage="validation",
                data_root=tmp_path / "data",
            )
        training, _ = queue_experiment(
            db,
            candidate_id=candidate_id,
            dataset_id="test-factor-v1",
            start=date(2024, 1, 2),
            end=date(2024, 1, 5),
            stage="training",
            data_root=tmp_path / "data",
        )
        training.status = "completed"
        training.result_json = json.dumps({"decision": "training_passed"})
        db.commit()
        with pytest.raises(ValueError, match="requires earlier, matching training_passed"):
            queue_experiment(
                db,
                candidate_id=candidate_id,
                dataset_id="test-factor-v1",
                start=date(2024, 1, 5),
                end=date(2024, 1, 10),
                stage="validation",
                data_root=tmp_path / "data",
            )
        experiment, _ = queue_experiment(
            db,
            candidate_id=candidate_id,
            dataset_id="test-factor-v1",
            start=date(2024, 1, 8),
            end=date(2024, 1, 10),
            stage="validation",
            data_root=tmp_path / "data",
        )
        experiment.status = "completed"
        experiment.result_json = json.dumps({"decision": "validation_rejected"})
        db.commit()
        assert serialize_experiment(experiment)["stage"] == "validation"
        memory = research_memory(db)
        assert len(memory["items"]) == 1
        assert all(item["experiment_id"] != experiment.id for item in memory["items"])
