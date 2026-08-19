"""
Tests for upload_idempotency.py — replaying a retried upload instead of duplicating it.
"""

import time
import uuid

import pytest

from app import upload_idempotency as idem


def _uuid7(ms: int | None = None) -> str:
    """
    Mint a syntactically valid UUIDv7. Python's stdlib has no uuid7() before 3.14 and
    the backend targets 3.12, so build one: 48-bit millisecond timestamp, version 7,
    variant 0b10, random elsewhere.
    """
    if ms is None:
        ms = int(time.time() * 1000)
    rand = uuid.uuid4().int
    value = (ms & ((1 << 48) - 1)) << 80
    value |= 7 << 76                                  # version
    value |= ((rand >> 4) & ((1 << 12) - 1)) << 64     # rand_a
    value |= 0b10 << 62                                # variant
    value |= rand & ((1 << 62) - 1)                    # rand_b
    return str(uuid.UUID(int=value))


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """Point the module at a throwaway database for each test."""
    monkeypatch.setattr(idem.settings, "data_dir", tmp_path)
    idem.close_db()
    idem.init_db()
    yield
    idem.close_db()


# --- key validation --------------------------------------------------------

def test_accepts_a_well_formed_uuid7():
    assert idem.is_valid_key(_uuid7())


def test_rejects_uuid4_because_it_carries_no_timestamp():
    assert not idem.is_valid_key(str(uuid.uuid4()))


def test_rejects_garbage():
    for bad in ["", "not-a-uuid", "12345", None]:
        assert not idem.is_valid_key(bad)


def test_recovers_the_timestamp_it_was_minted_with():
    ms = 1_754_300_000_000
    assert idem.uuid7_timestamp_ms(_uuid7(ms)) == ms


# --- claim / replay --------------------------------------------------------

def test_first_use_of_a_key_is_claimed():
    outcome, response = idem.begin(_uuid7(), "alice")
    assert outcome == "claimed"
    assert response is None


def test_second_request_while_the_first_is_running_is_told_in_progress():
    key = _uuid7()
    assert idem.begin(key, "alice")[0] == "claimed"
    assert idem.begin(key, "alice")[0] == "in_progress"


def test_a_finished_upload_replays_its_original_response():
    key = _uuid7()
    idem.begin(key, "alice")
    idem.finish(key, {"name": "photo.jpg", "sizeBytes": 42})

    outcome, response = idem.begin(key, "alice")
    assert outcome == "replayed"
    assert response == {"name": "photo.jpg", "sizeBytes": 42}


def test_replay_is_stable_across_repeated_retries():
    key = _uuid7()
    idem.begin(key, "alice")
    idem.finish(key, {"name": "photo.jpg"})
    for _ in range(3):
        assert idem.begin(key, "alice") == ("replayed", {"name": "photo.jpg"})


def test_a_failed_upload_releases_the_key_for_retry():
    key = _uuid7()
    idem.begin(key, "alice")
    idem.abandon(key)
    assert idem.begin(key, "alice")[0] == "claimed"


def test_abandon_does_not_erase_a_completed_upload():
    key = _uuid7()
    idem.begin(key, "alice")
    idem.finish(key, {"name": "photo.jpg"})
    idem.abandon(key)
    assert idem.begin(key, "alice")[0] == "replayed"


# --- isolation between users ----------------------------------------------

def test_another_user_cannot_read_someone_elses_result():
    key = _uuid7()
    idem.begin(key, "alice")
    idem.finish(key, {"name": "alice-private.jpg"})

    outcome, response = idem.begin(key, "mallory")
    assert outcome != "replayed"
    assert response is None


# --- stale claims and pruning ---------------------------------------------

def test_a_claim_from_a_dead_request_can_be_taken_over():
    key = _uuid7()
    idem.begin(key, "alice")
    # Age the claim past the staleness window without waiting for it.
    with idem._lock:
        idem._conn.execute(
            "UPDATE upload_keys SET created_at = ? WHERE key = ?",
            (time.time() - idem.STALE_CLAIM_SECONDS - 1, key),
        )
        idem._conn.commit()
    assert idem.begin(key, "alice")[0] == "claimed"


def test_prune_removes_only_keys_past_their_ttl():
    fresh, old = _uuid7(), _uuid7()
    idem.begin(fresh, "alice")
    idem.begin(old, "alice")
    with idem._lock:
        idem._conn.execute(
            "UPDATE upload_keys SET created_at = ? WHERE key = ?",
            (time.time() - idem.KEY_TTL_SECONDS - 1, old),
        )
        idem._conn.commit()

    assert idem.prune() == 1
    assert idem.begin(old, "alice")[0] == "claimed"      # gone, so freshly claimable
    assert idem.begin(fresh, "alice")[0] == "in_progress"  # still held


def test_a_corrupt_stored_response_does_not_wedge_the_client():
    key = _uuid7()
    idem.begin(key, "alice")
    idem.finish(key, {"name": "photo.jpg"})
    with idem._lock:
        idem._conn.execute(
            "UPDATE upload_keys SET response = ? WHERE key = ?", ("{not json", key)
        )
        idem._conn.commit()
    # Rather than replaying something unreadable, the upload is allowed to proceed.
    assert idem.begin(key, "alice")[0] == "claimed"
