"""H2d server-owned daily close receipt tests (all provider calls are fakes)."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from server.models.schema import (
    ManualPilotBinding,
    ManualPilotMarketReceipt,
    ManualProspectivePilot,
)
from server.services.manual_pilot_receipts import (
    FIELDS,
    PilotReceiptError,
    _fetch_provider_rows,
    capture_pilot_market_receipt,
    verify_pilot_market_receipt,
)
from server.services.strategy_promotion import create_strategy_release

NOW = datetime(2026, 9, 15, 9, 0, tzinfo=UTC)  # 17:00 Asia/Shanghai
SESSION = "2026-09-15"
CODES = ["000001.SZ", "600000.SH"]


def _raw_row(code: str, *, status: str = "1", close: str = "10.100000000000") -> dict[str, str]:
    exchange, number = code.split(".")
    return {
        "date": SESSION, "code": f"{exchange.lower()}.{number}", "open": "10.000000000000",
        "high": "10.500000000000", "low": "9.500000000000", "close": close,
        "preclose": "10.000000000000", "volume": "1000", "amount": "10000.123456789012",
        "turn": "0.123456789012", "tradestatus": status, "pctChg": "-1.234567890123", "isST": "0",
    }


def _raw(codes=CODES, **kwargs):
    return {"fields": list(FIELDS), "rows": [_raw_row(code, **kwargs) for code in codes]}


def _setup(db_session, monkeypatch, tmp_path, *, codes=CODES):
    release = create_strategy_release(
        db_session, strategy_key="receipt-test", version="v1", bundle_hash="a" * 64,
        strategy_fingerprint="b" * 64, research_evidence={"ok": True},
        execution_policy={"auto_submit": False}, risk_policy={"paper_only": True},
    )
    release.status = "paper_observing"
    db_session.commit()
    pilot = ManualProspectivePilot(
        id="pilot-receipt-1", pilot_key="pilot-receipt-key", release_id=release.id,
        strategy_fingerprint=release.strategy_fingerprint, start_date="2026-09-14",
        target_days=30, data_mode="real_forward", status="observing", observation_days=0,
        valid_days=0, required_evidence="{}", created_at="2026-09-14T09:00:00+00:00",
    )
    protocol = {
        "binding_version": "manual-pilot-binding-v1", "pilot_id": pilot.id,
        "binding_id": "binding-receipt-1", "started_at": "2026-09-14T09:00:00+00:00",
        "expected_dates": [SESSION], "universe": list(codes), "close_capture_not_before": "16:30:00",
    }
    binding = ManualPilotBinding(
        id="binding-receipt-1", pilot_id=pilot.id, release_id=release.id,
        holdout_evaluation_id="holdout-eval", holdout_evaluation_hash="c" * 64,
        holdout_artifact_id="holdout-artifact", holdout_artifact_hash="d" * 64,
        protocol_json=json.dumps(protocol, sort_keys=True), protocol_hash="e" * 64,
        binding_hash="f" * 64, request_key="binding-request", request_hash="1" * 64,
        created_by="operator", created_at="2026-09-14T09:00:00+00:00",
    )
    db_session.add_all([pilot, binding]); db_session.commit()
    monkeypatch.setattr("server.services.manual_pilot_receipts.verify_pilot_binding", lambda _db, _id: {
        "verified": True, "pilot_id": pilot.id, "binding_id": binding.id,
    })
    monkeypatch.setattr("server.services.manual_pilot_receipts._utc_now", lambda: NOW)
    monkeypatch.setattr("server.services.manual_pilot_receipts._fetch_provider_rows", lambda _codes, _day: _raw(_codes))
    monkeypatch.setattr("server.services.manual_pilot_receipts.settings.result_dir", str(tmp_path / "results"))
    return pilot, binding, release


def test_capture_and_verify_persists_exact_three_file_receipt(db_session, monkeypatch, tmp_path):
    pilot, _binding, _release = _setup(db_session, monkeypatch, tmp_path)
    receipt = capture_pilot_market_receipt(db_session, pilot_id=pilot.id, actor="operator", idempotency_key="receipt-key-1")
    checked = verify_pilot_market_receipt(db_session, receipt.id)
    assert checked["verified"] is True
    assert checked["receipt_id"] == receipt.id
    assert checked["normalized_rows"][0]["pctChg"] == "-1.234567890123"
    attempt = (tmp_path / "results" / receipt.manifest_path).parent
    assert {path.name for path in attempt.iterdir()} == {"raw.json", "normalized.json", "manifest.json"}
    assert db_session.query(ManualPilotMarketReceipt).count() == 1
    assert pilot.status == "observing"


def test_capture_rejects_before_close_and_does_not_fetch(db_session, monkeypatch, tmp_path):
    pilot, _binding, _release = _setup(db_session, monkeypatch, tmp_path)
    monkeypatch.setattr("server.services.manual_pilot_receipts._utc_now", lambda: datetime(2026, 9, 15, 7, 0, tzinfo=UTC))
    called = {"count": 0}
    monkeypatch.setattr("server.services.manual_pilot_receipts._fetch_provider_rows", lambda *_: called.__setitem__("count", called["count"] + 1))
    with pytest.raises(PilotReceiptError, match="window_not_open"):
        capture_pilot_market_receipt(db_session, pilot_id=pilot.id, actor="operator", idempotency_key="too-early")
    assert called["count"] == 0


@pytest.mark.parametrize("rows, error", [([], "partial_or_missing"), ([_raw_row("600000.SH"), _raw_row("600000.SH")], "duplicate"), ([_raw_row("600000.SH", close="NaN"), _raw_row("000001.SZ")], "non_finite")])
def test_capture_rejects_partial_duplicate_and_nonfinite_rows(db_session, monkeypatch, tmp_path, rows, error):
    pilot, _binding, _release = _setup(db_session, monkeypatch, tmp_path)
    monkeypatch.setattr("server.services.manual_pilot_receipts._fetch_provider_rows", lambda *_: {"fields": list(FIELDS), "rows": rows})
    with pytest.raises(PilotReceiptError, match=error):
        capture_pilot_market_receipt(db_session, pilot_id=pilot.id, actor="operator", idempotency_key=f"bad-{error}")


def test_suspended_zero_fields_are_preserved_as_explicit_suspension(db_session, monkeypatch, tmp_path):
    pilot, _binding, _release = _setup(db_session, monkeypatch, tmp_path, codes=["600000.SH"])
    monkeypatch.setattr("server.services.manual_pilot_receipts._fetch_provider_rows", lambda *_: {"fields": list(FIELDS), "rows": [_raw_row("600000.SH", status="0", close="0")]})
    receipt = capture_pilot_market_receipt(db_session, pilot_id=pilot.id, actor="operator", idempotency_key="suspended")
    row = verify_pilot_market_receipt(db_session, receipt.id)["normalized_rows"][0]
    assert row["is_suspended"] is True and row["tradestatus"] == 0 and row["close"] == "0"


def test_same_key_replay_verifies_without_second_fetch(db_session, monkeypatch, tmp_path):
    pilot, _binding, _release = _setup(db_session, monkeypatch, tmp_path)
    first = capture_pilot_market_receipt(db_session, pilot_id=pilot.id, actor="operator", idempotency_key="same-key")
    monkeypatch.setattr("server.services.manual_pilot_receipts._fetch_provider_rows", lambda *_: (_ for _ in ()).throw(AssertionError("network replay")))
    replay = capture_pilot_market_receipt(db_session, pilot_id=pilot.id, actor="operator", idempotency_key="same-key")
    assert replay.id == first.id


def test_request_key_is_bound_to_pilot_and_same_day_replacement_rejected(db_session, monkeypatch, tmp_path):
    pilot, _binding, _release = _setup(db_session, monkeypatch, tmp_path)
    capture_pilot_market_receipt(db_session, pilot_id=pilot.id, actor="operator", idempotency_key="first-key")
    with pytest.raises(PilotReceiptError, match="session_already_captured"):
        capture_pilot_market_receipt(db_session, pilot_id=pilot.id, actor="operator", idempotency_key="second-key")
    with pytest.raises(PilotReceiptError, match="idempotency_conflict"):
        capture_pilot_market_receipt(db_session, pilot_id="another-pilot", actor="operator", idempotency_key="first-key")


def test_manifest_or_database_anchor_tampering_is_rejected(db_session, monkeypatch, tmp_path):
    pilot, _binding, _release = _setup(db_session, monkeypatch, tmp_path)
    receipt = capture_pilot_market_receipt(db_session, pilot_id=pilot.id, actor="operator", idempotency_key="tamper-key")
    manifest = tmp_path / "results" / receipt.manifest_path
    payload = json.loads(manifest.read_text())
    payload["received_at"] = "2026-09-15T09:01:00+00:00"
    manifest.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    with pytest.raises(PilotReceiptError, match="manifest_hash_mismatch"):
        verify_pilot_market_receipt(db_session, receipt.id)


@pytest.mark.parametrize("field, value", [("received_at", "2026-09-15T09:01:00+00:00"), ("provider", "evil:provider")])
def test_database_identity_rewrite_is_rejected_by_manifest_identity(db_session, monkeypatch, tmp_path, field, value):
    pilot, _binding, _release = _setup(db_session, monkeypatch, tmp_path)
    receipt = capture_pilot_market_receipt(db_session, pilot_id=pilot.id, actor="operator", idempotency_key="db-time-key")
    setattr(receipt, field, value)
    db_session.commit()
    with pytest.raises(PilotReceiptError, match="manifest_identity_mismatch"):
        verify_pilot_market_receipt(db_session, receipt.id)


def test_symlink_manifest_is_rejected_before_following_target(db_session, monkeypatch, tmp_path):
    pilot, _binding, _release = _setup(db_session, monkeypatch, tmp_path)
    receipt = capture_pilot_market_receipt(db_session, pilot_id=pilot.id, actor="operator", idempotency_key="symlink-key")
    manifest = tmp_path / "results" / receipt.manifest_path
    target = tmp_path / "outside.json"
    target.write_text(manifest.read_text())
    manifest.unlink()
    manifest.symlink_to(target)
    with pytest.raises(PilotReceiptError, match="symlink"):
        verify_pilot_market_receipt(db_session, receipt.id)


def test_failed_db_append_leaves_orphan_and_retry_uses_new_attempt(db_session, monkeypatch, tmp_path):
    pilot, _binding, _release = _setup(db_session, monkeypatch, tmp_path)
    original_commit = db_session.commit
    failed = {"once": True}

    def fail_once():
        if failed["once"]:
            failed["once"] = False
            raise RuntimeError("append_failed")
        return original_commit()

    monkeypatch.setattr(db_session, "commit", fail_once)
    with pytest.raises(PilotReceiptError, match="persist_failed"):
        capture_pilot_market_receipt(db_session, pilot_id=pilot.id, actor="operator", idempotency_key="retry-key")
    monkeypatch.setattr(db_session, "commit", original_commit)
    receipt = capture_pilot_market_receipt(db_session, pilot_id=pilot.id, actor="operator", idempotency_key="retry-key")
    assert verify_pilot_market_receipt(db_session, receipt.id)["verified"] is True


def test_parent_mutation_during_fetch_blocks_receipt_without_db_row(db_session, monkeypatch, tmp_path):
    pilot, _binding, release = _setup(db_session, monkeypatch, tmp_path)

    def mutate_parent(codes, day):
        other = Session(bind=db_session.get_bind())
        try:
            other.get(type(release), release.id).status = "paper_passed"
            other.commit()
        finally:
            other.close()
        return _raw(codes)

    monkeypatch.setattr("server.services.manual_pilot_receipts._fetch_provider_rows", mutate_parent)
    with pytest.raises(PilotReceiptError, match="parent_changed"):
        capture_pilot_market_receipt(db_session, pilot_id=pilot.id, actor="operator", idempotency_key="parent-race")
    assert db_session.query(ManualPilotMarketReceipt).count() == 0


def test_expensive_admission_verify_never_runs_inside_receipt_lock(db_session, monkeypatch, tmp_path):
    pilot, binding, _release = _setup(db_session, monkeypatch, tmp_path)
    original_lock = __import__("server.services.manual_pilot_receipts", fromlist=["_lock_registry"])._lock_registry
    lock_active = {"value": False}

    def spy_verify(db, pilot_id):
        assert not lock_active["value"]
        return {"verified": True, "pilot_id": pilot.id, "binding_id": binding.id}

    def mark_lock(db):
        lock_active["value"] = True
        return original_lock(db)

    monkeypatch.setattr("server.services.manual_pilot_receipts.verify_pilot_binding", spy_verify)
    monkeypatch.setattr("server.services.manual_pilot_receipts._lock_registry", mark_lock)
    capture_pilot_market_receipt(db_session, pilot_id=pilot.id, actor="operator", idempotency_key="lock-spy")
    assert lock_active["value"] is True


def test_provider_query_uses_fixed_baostock_shape(monkeypatch):
    calls = []
    class FakeResult:
        def __init__(self):
            self.error_code = "0"
            self.error_msg = ""
            self.fields = list(FIELDS)
            self.data = [[]]
            self.cur_row_num = 1
            self._done = False
        def next(self):
            if self._done: return False
            self._done = True; return True
        def get_row_data(self): return list(_raw_row("600000.SH").values())
    class FakeBao:
        def login(self): return SimpleNamespace(error_code="0")
        def logout(self): pass
        def query_history_k_data_plus(self, *args, **kwargs): calls.append((args, kwargs)); return FakeResult()
    monkeypatch.setitem(__import__("sys").modules, "baostock", FakeBao())
    result = _fetch_provider_rows(["600000.SH"], datetime(2026, 9, 15, tzinfo=UTC).date())
    assert calls[0][0] == ("sh.600000",)
    assert calls[0][1]["adjustflag"] == "3" and calls[0][1]["start_date"] == SESSION
    assert result["fields"] == list(FIELDS)
