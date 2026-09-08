import json
from dataclasses import replace
from datetime import date
from unittest.mock import MagicMock, patch

import pytest
import pandas as pd
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from quant_engine.backtest.protocol import (
    AnalysisOutputSpec,
    DataRequirement,
    ParameterSpec,
    ParameterType,
    StrategyOutput,
    StrategyProtocolError,
    StrategySpec,
    normalize_strategy_output,
)
from quant_engine.backtest.registry import strategy_registry
from quant_engine.backtest.engine import BacktestEngine
from quant_engine.backtest.recorder import Recorder
from quant_engine.backtest.strategy import Strategy as StrategyBase
from server.api.backtest import BacktestRunRequest, _resolve_strategy_run, get_strategy_outputs
from server.api.strategies import (
    StrategyCreate,
    _strategy_response,
    create_strategy,
    get_strategy_catalog,
)
from server.models.database import Base
from server.models.schema import Run, Strategy
from server.services.strategy_evidence import strategy_fingerprint
from strategies.small_cap_value import SmallCapValueStrategy


def _spec(**changes):
    base = StrategySpec(
        id="protocol-test",
        name="Protocol test",
        version="1.0.0",
        description="Typed contract fixture",
        markets=("a-share",),
        parameters=(
            ParameterSpec("required_value", "Required", ParameterType.INTEGER, None, required=True),
            ParameterSpec("ratio", "Ratio", ParameterType.NUMBER, 0.5, minimum=0, maximum=1),
        ),
        analysis_outputs=(AnalysisOutputSpec("count", "Count", "integer"),),
    )
    return replace(base, **changes)


def test_required_parameter_can_be_declared_without_default():
    spec = _spec()
    with pytest.raises(StrategyProtocolError, match="missing required"):
        spec.validate_params()
    assert spec.validate_params({"required_value": 3}) == {
        "required_value": 3,
        "ratio": 0.5,
    }


def test_data_requirement_resolves_parameterized_lookback():
    requirement = DataRequirement(
        "a_share_daily", ("close",), 5,
        lookback_parameter="required_value", lookback_offset=2,
    )
    assert requirement.resolved_lookback({"required_value": 3}) == 5
    assert requirement.resolved_lookback({"required_value": 10}) == 12
    with pytest.raises(StrategyProtocolError, match="unknown data lookback parameter"):
        replace(_spec(), data=(replace(requirement, lookback_parameter="missing"),))


def test_optional_data_requirement_never_blocks_a_supported_engine():
    spec = replace(
        _spec(),
        data=(DataRequirement(
            "a_share_daily",
            ("optional_field",),
            100,
            optional=True,
        ),),
    )
    from quant_engine.backtest.protocol import resolve_strategy_data_requirements

    assert resolve_strategy_data_requirements(
        spec,
        {"required_value": 1},
        supported={"a_share_daily": ("1d", "event_driven")},
    ) == ()


def test_output_contract_rejects_undeclared_wrong_type_and_non_json_diagnostics():
    spec = _spec()
    with pytest.raises(StrategyProtocolError, match="undeclared"):
        normalize_strategy_output(StrategyOutput({}, {"other": 1}), spec=spec)
    with pytest.raises(StrategyProtocolError, match="must be an integer"):
        normalize_strategy_output(StrategyOutput({}, {"count": 1.5}), spec=spec)
    with pytest.raises(StrategyProtocolError, match="JSON serializable"):
        normalize_strategy_output(StrategyOutput({}, {"count": object()}), spec=spec)
    empty_spec = replace(spec, analysis_outputs=())
    with pytest.raises(StrategyProtocolError, match="undeclared"):
        normalize_strategy_output(StrategyOutput({}, {"count": 1}), spec=empty_spec)


def test_registry_discovers_builtin_specs_and_validates_loading():
    entries = strategy_registry.discover()
    assert {entry.spec.id for entry in entries} >= {"small-cap-value", "fund-nav-momentum"}
    loaded = strategy_registry.load("strategies.small_cap_value.SmallCapValueStrategy")
    assert loaded.strategy_class is SmallCapValueStrategy
    with pytest.raises(StrategyProtocolError, match="inside strategies package"):
        strategy_registry.load("os.system")


def test_strategy_api_catalog_and_create_use_typed_spec():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        catalog = get_strategy_catalog()
        assert catalog["protocol_version"] == "2.0"
        assert {item["id"] for item in catalog["strategies"]} >= {
            "small-cap-value", "fund-nav-momentum",
        }
        response = create_strategy(StrategyCreate(
            name="typed",
            strategy_class="strategies.small_cap_value.SmallCapValueStrategy",
            params={"top_n": 12},
            market="a-share",
        ), db)
        assert response.protocol_version == "2.0"
        assert response.spec_id == "small-cap-value"
        assert response.params == {"top_n": 12, "lookback": 30, "avoid_weak_months": True}
        stored = db.query(Strategy).filter(Strategy.id == response.id).one()
        assert json.loads(stored.params) == response.params
    finally:
        db.close()


def test_strategy_api_rejects_market_and_parameter_mismatch():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        with pytest.raises(HTTPException, match="does not support market"):
            create_strategy(StrategyCreate(
                name="wrong market",
                strategy_class="strategies.small_cap_value.SmallCapValueStrategy",
                params={},
                market="cn-fund",
            ), db)
        with pytest.raises(HTTPException, match="unknown strategy parameter"):
            create_strategy(StrategyCreate(
                name="wrong params",
                strategy_class="strategies.small_cap_value.SmallCapValueStrategy",
                params={"mystery": 1},
                market="a-share",
            ), db)
    finally:
        db.close()


def test_strategy_response_reports_real_legacy_compatibility():
    strategy = Strategy(
        id="stale",
        name="stale",
        strategy_class="strategies.small_cap_value.SmallCapValueStrategy",
        params='{"unknown": 1}',
        market="a-share",
        created_at="2024-01-01T00:00:00",
        updated_at="2024-01-01T00:00:00",
    )
    response = _strategy_response(strategy)
    assert response.protocol_compatible is False
    assert "unknown strategy parameter" in response.compatibility_error


def test_backtest_resolution_uses_spec_defaults_and_validates_overrides():
    strategy = Strategy(
        name="fund",
        strategy_class="strategies.fund_nav_momentum.FundNavMomentumStrategy",
        params='{"lookback": 10}',
        market="cn-fund",
    )
    request = BacktestRunRequest(
        strategy_id="fund-1",
        market="cn-fund",
        symbols=["110022"],
        start_date="2024-01-02",
        end_date="2024-01-31",
        parameter_overrides={"top_n": 2},
    )
    registered, params, frequency, source = _resolve_strategy_run(strategy, request)
    assert registered.spec.id == "fund-nav-momentum"
    assert params == {"lookback": 10, "top_n": 2, "target_weight": 1.0}
    assert (frequency, source) == ("daily", "strategy_spec")

    overridden = request.model_copy(update={"rebalance_frequency": "monthly"})
    assert _resolve_strategy_run(strategy, overridden)[2:] == ("monthly", "request_override")
    invalid = request.model_copy(update={"parameter_overrides": {"unknown": True}})
    with pytest.raises(StrategyProtocolError, match="unknown strategy parameter"):
        _resolve_strategy_run(strategy, invalid)


def test_fingerprint_includes_spec_version_and_default_normalized_params(monkeypatch):
    implementation = "strategies.small_cap_value.SmallCapValueStrategy"
    baseline = strategy_fingerprint(implementation, {"top_n": 10})
    explicit_defaults = strategy_fingerprint(implementation, {
        "top_n": 10,
        "lookback": 30,
        "avoid_weak_months": True,
    })
    assert baseline == explicit_defaults
    monkeypatch.setattr(
        SmallCapValueStrategy,
        "SPEC",
        replace(SmallCapValueStrategy.SPEC, version="2.0.1"),
    )
    assert strategy_fingerprint(implementation, {"top_n": 10}) != baseline


def test_strategy_outputs_endpoint_reads_generic_diagnostics(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        recorder = Recorder(output_dir=str(tmp_path))
        recorder.record_strategy_output(date(2024, 1, 2), {
            "count": 2,
            "details": {"state": "risk-on", "codes": ["000001.SZ"]},
        })
        recorder.save()
        run = Run(run_type="backtest", status="completed", result_dir=str(tmp_path))
        db.add(run)
        db.commit()
        db.refresh(run)
        result = get_strategy_outputs(run.id, db=db)
        assert result["total"] == 2
        assert result["outputs"][0]["key"] == "count"
        assert result["outputs"][0]["value"] == 2
        assert result["outputs"][1]["value"] == {
            "codes": ["000001.SZ"],
            "state": "risk-on",
        }
    finally:
        db.close()


def test_a_share_engine_fails_closed_when_declared_field_is_missing(tmp_path):
    class RequiredFieldStrategy(StrategyBase):
        SPEC = StrategySpec(
            id="required-field-test",
            name="Required field test",
            version="1.0.0",
            description="Requires turnover data",
            markets=("a-share",),
            data=(DataRequirement("a_share_daily", ("close", "turnover_rate"), 1),),
        )

        def initialize(self):
            pass

        def generate_signals(self, dt):
            return {}

    frame = pd.DataFrame([
        {"code": "000001.SZ", "date": date(2024, 1, 2), "open": 1.0,
         "high": 1.0, "low": 1.0, "close": 1.0, "volume": 100},
    ]).set_index(["code", "date"])
    api = MagicMock()
    api.daily.return_value = frame
    api.daily_coverage = None
    with patch("quant_engine.backtest.data_handler.DataAPI", return_value=api):
        with pytest.raises(ValueError, match="strategy_data_requirement_unsatisfied"):
            BacktestEngine(RequiredFieldStrategy, stock_list=["000001.SZ"]).run(
                start=date(2024, 1, 2),
                end=date(2024, 1, 2),
                rebalance_frequency="daily",
                output_dir=str(tmp_path),
            )


def test_a_share_engine_rejects_unsupported_frequency_and_adjustment(tmp_path):
    class UnsupportedDataStrategy(StrategyBase):
        SPEC = StrategySpec(
            id="unsupported-data-test",
            name="Unsupported data test",
            version="1.0.0",
            description="Requests unsupported intraday input",
            markets=("a-share",),
            data=(DataRequirement(
                "a_share_daily", ("close",), 1,
                frequency="1h", adjustment="none",
            ),),
        )

        def initialize(self):
            pass

        def generate_signals(self, dt):
            return {}

    frame = pd.DataFrame([
        {"code": "000001.SZ", "date": date(2024, 1, 2), "open": 1.0,
         "high": 1.0, "low": 1.0, "close": 1.0, "volume": 100},
    ]).set_index(["code", "date"])
    api = MagicMock()
    api.daily.return_value = frame
    api.daily_coverage = None
    with patch("quant_engine.backtest.data_handler.DataAPI", return_value=api):
        with pytest.raises(ValueError, match="strategy_data_requirement_unsupported"):
            BacktestEngine(UnsupportedDataStrategy, stock_list=["000001.SZ"]).run(
                start=date(2024, 1, 2),
                end=date(2024, 1, 2),
                rebalance_frequency="daily",
                output_dir=str(tmp_path),
            )
