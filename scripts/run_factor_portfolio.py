"""Run one validation-passed restricted factor through the research portfolio engine."""
from __future__ import annotations

import argparse
from datetime import date
import hashlib
import json
from pathlib import Path

import pandas as pd

from quant_engine.backtest.research_engine import ResearchBacktestEngine
from quant_engine.data.calendar import TradingCalendar
from quant_engine.factor.expression import FactorExpressionSpec
from quant_engine.factor.frozen import FrozenFactorStrategyBundle
from quant_engine.factor.strategy import build_expression_rank_strategy
from scripts.acquire_ashare_research import digest, write_json
from scripts.run_ashare_research import metrics
from server.models.database import SessionLocal, init_db
from server.models.schema import FactorCandidate, FactorExperiment


def _manifest_file(manifest_path: Path, item: dict) -> Path:
    configured = Path(str(item["path"]))
    path = next(
        (value for value in (configured, manifest_path.parent / configured.name) if value.exists()),
        None,
    )
    if path is None:
        raise FileNotFoundError(f"research input is missing: {configured.name}")
    if digest(path) != item["sha256"]:
        raise ValueError(f"research input hash mismatch: {configured.name}")
    return path


def _evidence_payload(row: FactorExperiment, decision: str) -> tuple[dict, dict[str, str]]:
    if row.status != "completed" or row.stage not in {"training", "validation"}:
        raise ValueError(f"factor evidence {row.id} has invalid status or stage")
    result = json.loads(row.result_json) if row.result_json else {}
    if result.get("decision") != decision:
        raise ValueError(f"factor evidence {row.id} does not contain {decision}")
    artifact_dir = Path(str(row.artifact_dir))
    artifacts = {}
    for name in (
        "report.json", "factor_values.parquet", "ic_series.parquet",
        "quantile_returns.parquet",
    ):
        path = artifact_dir / name
        if not path.is_file():
            raise FileNotFoundError(f"factor evidence artifact is missing: {path}")
        artifacts[name] = digest(path)
    if json.loads((artifact_dir / "report.json").read_text(encoding="utf-8")) != result:
        raise ValueError(f"factor evidence DB/report mismatch: {row.id}")
    return result, artifacts


def _candidate_bundle(
    candidate_id: str,
    training_experiment_id: str,
    validation_experiment_id: str,
    *,
    top_n: int,
    gross_exposure: float,
    rebalance_frequency: str,
) -> FrozenFactorStrategyBundle:
    code_hashes = {
        path: digest(Path(path)) for path in (
            "quant_engine/backtest/protocol.py",
            "quant_engine/backtest/research_engine.py",
            "quant_engine/backtest/research_ledger.py",
            "quant_engine/factor/expression.py",
            "quant_engine/factor/frozen.py",
            "quant_engine/factor/strategy.py",
            "scripts/run_factor_portfolio.py",
        )
    }
    init_db()
    with SessionLocal() as db:
        candidate = db.get(FactorCandidate, candidate_id)
        if candidate is None:
            raise KeyError(f"factor candidate not found: {candidate_id}")
        training = db.get(FactorExperiment, training_experiment_id)
        validation = db.get(FactorExperiment, validation_experiment_id)
        if training is None or validation is None:
            raise KeyError("explicit factor training/validation evidence was not found")
        if training.candidate_id != candidate_id or validation.candidate_id != candidate_id:
            raise ValueError("factor evidence belongs to a different candidate")
        if training.stage != "training" or validation.stage != "validation":
            raise ValueError("factor evidence stages must be training then validation")
        training_result, training_artifacts = _evidence_payload(
            training, "training_passed"
        )
        validation_result, validation_artifacts = _evidence_payload(
            validation, "validation_passed"
        )
        if date.fromisoformat(training.end_date) >= date.fromisoformat(validation.start_date):
            raise ValueError("factor training and validation periods overlap")
        identity_fields = (
            "dataset_id", "data_content_hash", "forward_horizon", "evaluation_policy"
        )
        if any(getattr(training, key) != getattr(validation, key) for key in identity_fields):
            raise ValueError("factor training and validation evidence protocols do not match")
        expression = FactorExpressionSpec.from_dict(json.loads(candidate.expression_spec))
        if any(
            result.get("expression_hash") != expression.expression_hash
            for result in (training_result, validation_result)
        ):
            raise ValueError("factor evidence expression hash does not match the candidate")
        return FrozenFactorStrategyBundle(
            expression=expression,
            training_experiment_id=training.id,
            validation_experiment_id=validation.id,
            training_result_hash=hashlib.sha256(training.result_json.encode()).hexdigest(),
            validation_result_hash=hashlib.sha256(validation.result_json.encode()).hexdigest(),
            training_artifact_hashes=training_artifacts,
            validation_artifact_hashes=validation_artifacts,
            dataset_id=training.dataset_id,
            data_content_hash=training.data_content_hash,
            forward_horizon=training.forward_horizon,
            evaluation_policy=json.loads(training.evaluation_policy),
            code_hashes=code_hashes,
            top_n=top_n,
            gross_exposure=gross_exposure,
            rebalance_frequency=rebalance_frequency,
        )


def run(
    *,
    candidate_id: str,
    training_experiment_id: str,
    validation_experiment_id: str,
    manifest_path: Path,
    output_dir: Path,
    start: date,
    end: date,
    top_n: int = 50,
    gross_exposure: float = 0.90,
    rebalance_frequency: str = "weekly",
    cost_scenario: str = "baseline",
) -> dict:
    if output_dir.exists():
        raise FileExistsError("factor portfolio output already exists; choose a new directory")
    bundle = _candidate_bundle(
        candidate_id,
        training_experiment_id,
        validation_experiment_id,
        top_n=top_n,
        gross_exposure=gross_exposure,
        rebalance_frequency=rebalance_frequency,
    )
    expression = bundle.expression
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        manifest.get("dataset_id") != bundle.dataset_id
        or manifest.get("content_hash") != bundle.data_content_hash
    ):
        raise ValueError("portfolio manifest does not match the frozen factor evidence")
    inputs = {
        name: pd.read_parquet(_manifest_file(manifest_path, manifest["files"][name]))
        for name in ("daily", "actions", "securities")
    }
    strategy_class = build_expression_rank_strategy(
        expression,
        rebalance_frequency=rebalance_frequency,
        bundle_hash=bundle.bundle_hash,
    )
    identity = {
        "protocol_version": "factor-portfolio-v1",
        "bundle": bundle.as_dict(),
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "top_n": top_n,
        "gross_exposure": gross_exposure,
        "rebalance_frequency": rebalance_frequency,
        "cost_scenario": cost_scenario,
    }
    run_id = hashlib.sha256(
        json.dumps(identity, sort_keys=True).encode()
    ).hexdigest()[:32]
    engine = ResearchBacktestEngine(
        strategy_class,
        parameters={"top_n": top_n, "gross_exposure": gross_exposure},
        cost_scenario=cost_scenario,
        factor_expressions={expression.name: expression},
    )
    engine.run(
        **inputs,
        start=start,
        end=end,
        calendar=TradingCalendar(start_year=2013, end_year=2026),
        output_dir=str(output_dir),
        initial_capital=1_000_000.0,
        rebalance_frequency=rebalance_frequency,
    )
    summary_path = output_dir / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary.update({
        "factor_portfolio": True,
        "factor_portfolio_protocol": "factor-portfolio-v1",
        "factor_candidate_id": candidate_id,
        "factor_expression_hash": expression.expression_hash,
        "factor_strategy_bundle_hash": bundle.bundle_hash,
        "factor_evidence_experiment_ids": [
            bundle.training_experiment_id, bundle.validation_experiment_id,
        ],
        "data_content_hash": manifest["content_hash"],
        "dataset_id": manifest["dataset_id"],
        "portfolio_validation_period_reuses_factor_validation_data": True,
        "paper_authorized": False,
        "validated": False,
    })
    limitations = list(summary.get("limitations", []))
    limitations.extend([
        "factor_training_and_validation_do_not_authorize_paper_or_live_execution",
        "portfolio_period_reuses_previously_opened_factor_validation_data",
        "no_new_sealed_holdout_is_available_in_the_current_dataset",
    ])
    summary["limitations"] = list(dict.fromkeys(limitations))
    run_metrics = metrics(output_dir)
    summary["factor_portfolio_metrics"] = run_metrics
    write_json(summary_path, summary)
    write_json(output_dir / "factor-strategy-bundle.json", bundle.as_dict())
    report = {
        "run_id": run_id,
        "identity": identity,
        "result_dir": str(output_dir.resolve()),
        "metrics": run_metrics,
        "validated": False,
        "limitations": summary["limitations"],
    }
    write_json(output_dir / "factor-portfolio.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--training-experiment-id", required=True)
    parser.add_argument("--validation-experiment-id", required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--top-n", type=int, default=50)
    parser.add_argument("--gross-exposure", type=float, default=0.90)
    parser.add_argument("--frequency", choices=("weekly", "monthly"), default="weekly")
    parser.add_argument("--cost", choices=("baseline", "stress"), default="baseline")
    args = parser.parse_args()
    report = run(
        candidate_id=args.candidate_id,
        training_experiment_id=args.training_experiment_id,
        validation_experiment_id=args.validation_experiment_id,
        manifest_path=args.manifest,
        output_dir=args.output,
        start=args.start,
        end=args.end,
        top_n=args.top_n,
        gross_exposure=args.gross_exposure,
        rebalance_frequency=args.frequency,
        cost_scenario=args.cost,
    )
    print(json.dumps(report, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
