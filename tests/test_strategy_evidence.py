import json

from server.services.strategy_evidence import (
    EVIDENCE_VERSION,
    EXECUTION_MODEL,
    build_manifest,
    manifest_is_complete,
    serialize_manifest,
    strategy_fingerprint,
)


def test_strategy_fingerprint_is_deterministic_and_parameter_sensitive():
    first = strategy_fingerprint("strategies.small_cap_value.SmallCapValueStrategy", {"top_n": 10})
    reordered = strategy_fingerprint("strategies.small_cap_value.SmallCapValueStrategy", json.dumps({"top_n": 10}))
    changed = strategy_fingerprint("strategies.small_cap_value.SmallCapValueStrategy", {"top_n": 11})
    assert first == reordered
    assert first != changed
    assert len(first) == 64


def test_manifest_round_trip_has_observation_gate_fields():
    manifest = build_manifest(
        market="a-share",
        strategy_class="strategies.small_cap_value.SmallCapValueStrategy",
        params={"top_n": 10},
        start_date="2024-01-01",
        end_date="2024-12-31",
        benchmark="000300.SH",
        rebalance_frequency="weekly",
    )
    manifest["calendar_evidence"] = {
        "source": "test:calendar",
        "content_hash": "a" * 64,
        "coverage_start": "2024-01-01",
        "coverage_end": "2024-12-31",
        "verified": True,
    }
    encoded = serialize_manifest(manifest)
    assert json.loads(encoded)["evidence_version"] == EVIDENCE_VERSION
    assert json.loads(encoded)["execution_model"] == EXECUTION_MODEL == "next_trading_day_open-v1"
    assert manifest_is_complete(encoded)


def test_legacy_close_execution_manifest_remains_readable():
    manifest = build_manifest(
        market="a-share",
        strategy_class="strategies.small_cap_value.SmallCapValueStrategy",
        params={}, start_date="2024-01-01", end_date="2024-01-02",
        benchmark=None, rebalance_frequency="daily",
    )
    manifest["calendar_evidence"] = {
        "source": "test:calendar",
        "content_hash": "b" * 64,
        "coverage_start": "2024-01-01",
        "coverage_end": "2024-01-02",
        "verified": True,
    }
    manifest["execution_model"] = "next_trading_day_close-v1"
    assert manifest_is_complete(manifest)


def test_manifest_without_verified_calendar_is_incomplete():
    manifest = build_manifest(
        market="a-share",
        strategy_class="strategies.small_cap_value.SmallCapValueStrategy",
        params={}, start_date="2024-01-01", end_date="2024-01-02",
        benchmark=None, rebalance_frequency="daily",
    )
    assert not manifest_is_complete(manifest)
