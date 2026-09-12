"""
Store module tests — JSON persistence, atomic writes, caching,
token purge, OTP lifecycle, and corrupt file recovery.
"""

import asyncio
import json
import pytest
from pathlib import Path


@pytest.mark.asyncio
async def test_store_users_crud(tmp_path, monkeypatch):
    """Create, find, and remove users."""
    monkeypatch.setenv("AHC_DATA_DIR", str(tmp_path))
    from app.config import settings
    from app import store
    settings.data_dir = tmp_path
    store._cache.clear()

    # Empty initially
    users = await store.get_users()
    assert users == []

    # Add user
    user = await store.add_user("alice", pin="hashed_pin", is_admin=True)
    assert user["name"] == "alice"
    assert user["is_admin"] is True

    # Find user
    found = await store.find_user(user["id"])
    assert found is not None
    assert found["name"] == "alice"

    # Update PIN
    ok = await store.update_user_pin(user["id"], "new_hashed_pin")
    assert ok is True
    found = await store.find_user(user["id"])
    assert found["pin"] == "new_hashed_pin"

    # Remove user
    ok = await store.remove_user(user["id"])
    assert ok is True
    found = await store.find_user(user["id"])
    assert found is None

    # Remove nonexistent
    ok = await store.remove_user("no_such_id")
    assert ok is False

    store._cache.clear()


@pytest.mark.asyncio
async def test_store_services_defaults(tmp_path, monkeypatch):
    """Services should auto-create defaults when file doesn't exist."""
    monkeypatch.setenv("AHC_DATA_DIR", str(tmp_path))
    from app.config import settings
    from app import store
    settings.data_dir = tmp_path
    store._cache.clear()

    services = await store.get_services()
    assert isinstance(services, list)
    assert len(services) == 4  # dlna, smb, nfs, ssh are defaults
    ids = [s["id"] for s in services]
    assert set(ids) == {"dlna", "smb", "nfs", "ssh"}
    nfs = next(s for s in services if s["id"] == "nfs")
    assert nfs["isEnabled"] is False  # off by default until exports are configured

    store._cache.clear()


@pytest.mark.asyncio
async def test_store_toggle_service(tmp_path, monkeypatch):
    """Toggle a service's enabled state."""
    monkeypatch.setenv("AHC_DATA_DIR", str(tmp_path))
    from app.config import settings
    from app import store
    settings.data_dir = tmp_path
    store._cache.clear()

    # Initialize
    await store.get_services()

    # Toggle smb off
    ok = await store.toggle_service("smb", False)
    assert ok is True

    # Verify
    services = await store.get_services()
    smb = next(s for s in services if s["id"] == "smb")
    assert smb["isEnabled"] is False

    # Toggle nonexistent
    ok = await store.toggle_service("nonexistent", True)
    assert ok is False

    store._cache.clear()


@pytest.mark.asyncio
async def test_store_services_media_migration_splits_into_dlna_and_smb(tmp_path, monkeypatch):
    """A legacy services file with a unified 'media' entry (and no 'nfs') should be
    migrated into distinct dlna + smb entries (preserving the enabled state) plus a
    new, disabled-by-default nfs entry — without losing the user's ssh setting."""
    monkeypatch.setenv("AHC_DATA_DIR", str(tmp_path))
    from app.config import settings
    from app import store
    settings.data_dir = tmp_path
    store._cache.clear()

    legacy = [
        {"id": "media", "name": "TV & Computer Sharing",
         "description": "DLNA streaming + SMB file sharing", "isEnabled": False},
        {"id": "ssh", "name": "SSH", "description": "Secure remote terminal", "isEnabled": True},
    ]
    store._write_json(settings.services_file, legacy)

    services = await store.get_services()
    ids = {s["id"] for s in services}
    assert ids == {"dlna", "smb", "nfs", "ssh"}

    dlna = next(s for s in services if s["id"] == "dlna")
    smb = next(s for s in services if s["id"] == "smb")
    nfs = next(s for s in services if s["id"] == "nfs")
    ssh = next(s for s in services if s["id"] == "ssh")
    assert dlna["isEnabled"] is False  # inherited from the legacy media entry
    assert smb["isEnabled"] is False   # inherited from the legacy media entry
    assert nfs["isEnabled"] is False   # new, not derived from anything legacy
    assert ssh["isEnabled"] is True    # untouched by the migration

    store._cache.clear()


@pytest.mark.asyncio
async def test_store_services_repeated_reads_do_not_duplicate_after_migration(tmp_path, monkeypatch):
    """Regression test: found live 2026-07-14 verifying the SMB/NFS toggle feature.
    The old samba/dlna->media merge migration and the new media->dlna+smb split
    migration ping-ponged forever on the modern (post-split) shape, because the
    merge migration only checked for a standalone 'dlna' (which is now a
    permanent, non-legacy id) without also checking 'smb' was absent — each
    full read+migrate cycle (i.e. each cache-expiry re-read, _CACHE_TTL=5s in
    production) added one more duplicate 'smb' entry, forever. Simulate that by
    clearing the cache between reads, exactly as a TTL expiry would."""
    monkeypatch.setenv("AHC_DATA_DIR", str(tmp_path))
    from app.config import settings
    from app import store
    settings.data_dir = tmp_path
    store._cache.clear()

    # First read creates the fresh, already-split default shape.
    first = await store.get_services()
    assert [s["id"] for s in first] == ["dlna", "smb", "nfs", "ssh"]

    # Simulate several cache-expiry cycles re-reading the same on-disk file.
    for _ in range(5):
        store._cache.clear()
        again = await store.get_services()
        ids = [s["id"] for s in again]
        assert ids == ["dlna", "smb", "nfs", "ssh"], f"duplicated on re-read: {ids}"

    store._cache.clear()


@pytest.mark.asyncio
async def test_store_device_state(tmp_path, monkeypatch):
    """Device name read/write."""
    monkeypatch.setenv("AHC_DATA_DIR", str(tmp_path))
    from app.config import settings
    from app import store
    settings.data_dir = tmp_path
    store._cache.clear()

    state = await store.get_device_state()
    assert "name" in state

    await store.update_device_name("TestDevice")
    state = await store.get_device_state()
    assert state["name"] == "TestDevice"

    store._cache.clear()


@pytest.mark.asyncio
async def test_store_storage_state_lifecycle(tmp_path, monkeypatch):
    """Save and clear storage state."""
    monkeypatch.setenv("AHC_DATA_DIR", str(tmp_path))
    from app.config import settings
    from app import store
    settings.data_dir = tmp_path
    store._cache.clear()

    # Initially empty
    state = await store.get_storage_state()
    assert state == {}

    # Save
    await store.save_storage_state({"activeDevice": "/dev/sda1"})
    state = await store.get_storage_state()
    assert state["activeDevice"] == "/dev/sda1"

    # Clear
    await store.clear_storage_state()
    state = await store.get_storage_state()
    assert state == {}

    store._cache.clear()


@pytest.mark.asyncio
async def test_store_tokens_add_get_revoke(tmp_path, monkeypatch):
    """Token CRUD and revocation."""
    monkeypatch.setenv("AHC_DATA_DIR", str(tmp_path))
    from app.config import settings
    from app import store
    settings.data_dir = tmp_path
    store._cache.clear()

    # Initially empty
    tokens = await store.get_tokens()
    assert tokens == []

    # Add
    record = {"jti": "abc123", "userId": "user_1", "expiresAt": 9999999999, "revoked": False}
    await store.add_token(record)
    found = await store.get_token("abc123")
    assert found is not None
    assert found["jti"] == "abc123"

    # Revoke
    ok = await store.revoke_token("abc123")
    assert ok is True
    found = await store.get_token("abc123")
    assert found["revoked"] is True

    # Revoke nonexistent
    ok = await store.revoke_token("no_such_jti")
    assert ok is False

    store._cache.clear()


@pytest.mark.asyncio
async def test_store_token_purge_uses_correct_key(tmp_path, monkeypatch):
    """Purge should use 'expiresAt' key (camelCase) matching auth.py."""
    monkeypatch.setenv("AHC_DATA_DIR", str(tmp_path))
    from app.config import settings
    from app import store
    settings.data_dir = tmp_path
    store._cache.clear()

    # Add an expired token (expiresAt in the past)
    await store.add_token({"jti": "old", "userId": "u1", "expiresAt": 1000, "revoked": False})
    await store.add_token({"jti": "fresh", "userId": "u2", "expiresAt": 9999999999, "revoked": False})

    # Purge tokens older than now
    removed = await store.purge_expired_tokens(2000)
    assert removed == 1

    # Only fresh should remain
    tokens = await store.get_tokens()
    assert len(tokens) == 1
    assert tokens[0]["jti"] == "fresh"

    store._cache.clear()


@pytest.mark.asyncio
async def test_store_otp_lifecycle(tmp_path, monkeypatch):
    """OTP save, get, clear cycle."""
    monkeypatch.setenv("AHC_DATA_DIR", str(tmp_path))
    from app.config import settings
    from app import store
    settings.data_dir = tmp_path
    store._cache.clear()

    # Initially empty
    otp = await store.get_otp()
    assert otp is None

    # Save
    await store.save_otp("hash123", 9999999999)
    otp = await store.get_otp()
    assert otp is not None
    assert otp["otp_hash"] == "hash123"

    # Clear
    await store.clear_otp()
    store._cache.clear()  # force re-read from disk
    otp = await store.get_otp()
    # After clear, file contains {} which is falsy — get_otp returns None
    assert otp is None

    store._cache.clear()


@pytest.mark.asyncio
async def test_store_corrupt_json_recovery(tmp_path, monkeypatch):
    """Corrupt JSON file should be recovered gracefully."""
    monkeypatch.setenv("AHC_DATA_DIR", str(tmp_path))
    from app.config import settings
    from app import store
    settings.data_dir = tmp_path
    store._cache.clear()

    # Write corrupt JSON to users.json
    users_file = tmp_path / "users.json"
    users_file.write_text("{invalid json content!!!}")

    # Reading should return default, not crash
    users = await store.get_users()
    assert users == []

    # Corrupt file should be renamed
    assert (tmp_path / "users.json.corrupt").exists()

    store._cache.clear()


@pytest.mark.asyncio
async def test_atomic_write_survives_concurrent_access(tmp_path, monkeypatch):
    """Multiple concurrent writes should not corrupt the store."""
    import asyncio
    monkeypatch.setenv("AHC_DATA_DIR", str(tmp_path))
    from app.config import settings
    from app import store
    settings.data_dir = tmp_path
    store._cache.clear()

    async def add_user(name):
        await store.add_user(name)

    # Run 10 concurrent user additions
    tasks = [add_user(f"user_{i}") for i in range(10)]
    await asyncio.gather(*tasks)

    users = await store.get_users()
    # All users should have been added (asyncio.Lock protects writes)
    assert len(users) == 10

    store._cache.clear()


@pytest.mark.asyncio
async def test_add_user_icon_emoji_is_stored(tmp_path, monkeypatch):
    """TASK-014: add_user stores icon_emoji and get_users returns it."""
    monkeypatch.setenv("AHC_DATA_DIR", str(tmp_path))
    from app.config import settings
    from app import store
    settings.data_dir = tmp_path
    store._cache.clear()

    user = await store.add_user("emoji_user", pin=None, icon_emoji="\U0001f3e0")
    assert user["icon_emoji"] == "\U0001f3e0", "icon_emoji must be present in returned dict"

    # Verify it persists through get_users()
    users = await store.get_users()
    found = next((u for u in users if u["name"] == "emoji_user"), None)
    assert found is not None
    assert found["icon_emoji"] == "\U0001f3e0", "icon_emoji must survive round-trip through store"

    store._cache.clear()


@pytest.mark.asyncio
async def test_add_user_icon_emoji_defaults_to_empty_string(tmp_path, monkeypatch):
    """TASK-014: add_user without icon_emoji stores empty string, not None."""
    monkeypatch.setenv("AHC_DATA_DIR", str(tmp_path))
    from app.config import settings
    from app import store
    settings.data_dir = tmp_path
    store._cache.clear()

    user = await store.add_user("plain_user", pin=None)
    assert user.get("icon_emoji", None) == "", (
        "icon_emoji must default to empty string when not supplied"
    )

    store._cache.clear()


@pytest.mark.asyncio
async def test_get_value_caches_none_correctly(tmp_path, monkeypatch):
    """get_value must return None for a key whose stored value IS None without forcing
    a re-read on every call (regression: old code used 'if cached is not None')."""
    monkeypatch.setenv("AHC_DATA_DIR", str(tmp_path))
    from app.config import settings
    from app import store
    settings.data_dir = tmp_path
    store._cache.clear()

    # Write None as the value for a key
    await store.set_value("nullable_key", None)

    # Write it back as None explicitly so the JSON has the key with null
    import json
    kv_path = tmp_path / "kv.json"
    kv_path.write_text(json.dumps({"nullable_key": None}))
    store._cache.clear()

    # First read should return None (from disk)
    result = await store.get_value("nullable_key", default="MISSING")
    assert result is None, "get_value must return None for a null JSON value, not the default"

    # Second read with the key absent from JSON should return default
    await store.set_value("other_key", "hello")
    result2 = await store.get_value("nonexistent_key", default="fallback")
    assert result2 == "fallback", "get_value must return the default for a missing key"

    store._cache.clear()


# ---------------------------------------------------------------------------
# Activity log (activity_log.json)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_activity_log_empty_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("AHC_DATA_DIR", str(tmp_path))
    from app.config import settings
    from app import store
    settings.data_dir = tmp_path
    store._cache.clear()

    events = await store.get_activity_events()
    assert events == []

    store._cache.clear()


@pytest.mark.asyncio
async def test_activity_log_append_inserts_newest_first(tmp_path, monkeypatch):
    monkeypatch.setenv("AHC_DATA_DIR", str(tmp_path))
    from app.config import settings
    from app import store
    settings.data_dir = tmp_path
    store._cache.clear()

    await store.append_activity_event({"event": "first", "timestamp": 1.0})
    await store.append_activity_event({"event": "second", "timestamp": 2.0})
    await store.append_activity_event({"event": "third", "timestamp": 3.0})

    events = await store.get_activity_events()
    assert [e["event"] for e in events] == ["third", "second", "first"]

    store._cache.clear()


@pytest.mark.asyncio
async def test_activity_log_caps_at_max_entries(tmp_path, monkeypatch):
    """Regression pin: the persisted log must never grow unbounded over a
    device's lifetime -- append past the cap and the oldest entries should be
    dropped, not accumulate forever."""
    monkeypatch.setenv("AHC_DATA_DIR", str(tmp_path))
    from app.config import settings
    from app import store
    settings.data_dir = tmp_path
    store._cache.clear()

    original_max = store._ACTIVITY_LOG_MAX_ENTRIES
    store._ACTIVITY_LOG_MAX_ENTRIES = 5
    try:
        for i in range(8):
            await store.append_activity_event({"event": f"event-{i}", "timestamp": float(i)})

        events = await store.get_activity_events()
        assert len(events) == 5
        # Newest-first: the 5 most recent (event-7 down to event-3) survive,
        # the oldest 3 (event-0, event-1, event-2) are dropped.
        assert [e["event"] for e in events] == [
            "event-7", "event-6", "event-5", "event-4", "event-3",
        ]
    finally:
        store._ACTIVITY_LOG_MAX_ENTRIES = original_max
        store._cache.clear()


@pytest.mark.asyncio
async def test_activity_log_persists_across_cache_clear(tmp_path, monkeypatch):
    monkeypatch.setenv("AHC_DATA_DIR", str(tmp_path))
    from app.config import settings
    from app import store
    settings.data_dir = tmp_path
    store._cache.clear()

    await store.append_activity_event({"event": "file_deleted", "timestamp": 1.0, "actor_id": "user1"})
    store._cache.clear()

    events = await store.get_activity_events()
    assert len(events) == 1
    assert events[0]["actor_id"] == "user1"

    store._cache.clear()


# ---------------------------------------------------------------------------
# Trash metadata — concurrent read-modify-write races
# ---------------------------------------------------------------------------
# add_trash_item/remove_trash_item/mutate_trash_items each do their read-modify-write under
# ONE lock acquisition. Callers that instead did a get_trash_items() + save_trash_items() pair
# (trash restore/delete, the quota/age purge job, the Telegram empty-trash and dup-review-trash
# flows) had a real window for a concurrent caller's change to be silently discarded, because
# each call released the lock between the read and the write.

@pytest.mark.asyncio
async def test_old_get_then_save_pair_can_lose_a_concurrent_update(tmp_path, monkeypatch):
    """Reproduces the vulnerable pattern directly (not from application code, since it's been
    fixed everywhere) -- two callers each do get_trash_items() -> mutate in Python ->
    save_trash_items(), with a real await-yield forced between the read and the write, exactly
    where the two separate lock acquisitions used to leave a window open."""
    monkeypatch.setenv("AHC_DATA_DIR", str(tmp_path))
    from app.config import settings
    from app import store
    settings.data_dir = tmp_path
    store._cache.clear()

    async def racy_add(new_id):
        items = await store.get_trash_items()
        await asyncio.sleep(0)  # yield -- lets the other racy_add's read run in between
        items = items + [{"id": new_id}]
        await store.save_trash_items(items)

    await asyncio.gather(racy_add("A"), racy_add("B"))

    stored = await store.get_trash_items()
    ids = {i["id"] for i in stored}
    assert ids != {"A", "B"}, "expected the vulnerable racy pattern to lose one of the two writes"

    store._cache.clear()


@pytest.mark.asyncio
async def test_concurrent_add_trash_item_never_loses_an_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("AHC_DATA_DIR", str(tmp_path))
    from app.config import settings
    from app import store
    settings.data_dir = tmp_path
    store._cache.clear()

    items = [{"id": f"item-{i}"} for i in range(20)]
    await asyncio.gather(*(store.add_trash_item(it) for it in items))

    stored = await store.get_trash_items()
    assert {i["id"] for i in stored} == {f"item-{i}" for i in range(20)}

    store._cache.clear()


@pytest.mark.asyncio
async def test_mutate_trash_items_and_a_concurrent_add_do_not_clobber_each_other(tmp_path, monkeypatch):
    """A bulk mutate (purge/empty-trash) racing a single add (soft-delete/dup-review-trash)
    must not lose either change, regardless of which happens to run first."""
    monkeypatch.setenv("AHC_DATA_DIR", str(tmp_path))
    from app.config import settings
    from app import store
    settings.data_dir = tmp_path
    store._cache.clear()

    await store.add_trash_item({"id": "seed"})

    await asyncio.gather(
        store.mutate_trash_items(lambda items: [i for i in items if i["id"] != "seed"]),
        store.add_trash_item({"id": "new"}),
    )

    stored = await store.get_trash_items()
    assert {i["id"] for i in stored} == {"new"}, (
        "the concurrent add must survive the mutate, and the mutate's removal must not be "
        "reverted by a stale snapshot from the add"
    )

    store._cache.clear()
