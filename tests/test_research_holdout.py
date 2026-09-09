"""M4 sealed/opened/invalidated holdout lifecycle tests."""
import pytest

from server.services.research_holdout import (
    HoldoutError,
    access_holdout,
    complete_holdout,
    create_holdout_window,
    holdout_accesses,
    invalidate_holdout,
)


def test_holdout_opening_is_recorded_and_cannot_be_resealed(db_session):
    window = create_holdout_window(
        db_session, dataset_id="dataset-v2", data_content_hash="1" * 64,
        start_date="2027-01-01", end_date="2027-06-30", policy_hash="2" * 64,
    )
    access = access_holdout(db_session, window.id, accessed_by="worker", purpose="manual validation")
    assert access.result_exposed is True
    assert db_session.get(type(window), window.id).status == "opened"
    assert len(holdout_accesses(db_session, window.id)) == 1
    complete_holdout(db_session, window.id)
    with pytest.raises(HoldoutError, match="completed"):
        access_holdout(db_session, window.id, accessed_by="worker", purpose="late read")


def test_invalidated_holdout_cannot_complete(db_session):
    window = create_holdout_window(
        db_session, dataset_id="dataset-v3", data_content_hash="3" * 64,
        start_date="2027-01-01", end_date="2027-01-02", policy_hash="4" * 64,
    )
    invalidate_holdout(db_session, window.id, reason="policy changed")
    with pytest.raises(HoldoutError, match="invalidated"):
        complete_holdout(db_session, window.id)
