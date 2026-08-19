"""
Ingest core tests — the unified file-write path shared by app upload,
Telegram, phone sync, and the web portal (app/ingest.py).
"""

import asyncio

import pytest

from app.ingest import (
    Destination,
    IngestDiskFullError,
    IngestMode,
    IngestSizeError,
    IngestStallError,
    Scope,
    ingest,
)


async def _chunks(*parts: bytes):
    for p in parts:
        yield p


async def _stalling_chunks(first: bytes):
    yield first
    await asyncio.sleep(10)  # far longer than the tiny stall_timeout_s used in tests
    yield b"never gets here"


@pytest.mark.asyncio
async def test_ingest_writes_and_sorts(client):
    """A photo with no explicit subpath lands under Photos/ (synchronous sort)."""
    dest = Destination(scope=Scope.FAMILY, mode=IngestMode.SORTED)
    result = await ingest(_chunks(b"hello world"), filename="pic.jpg", dest=dest)

    assert result.bytes_written == len(b"hello world")
    assert result.dedup_hit is False
    assert result.sorted_to == "Photos"
    assert result.path.name == "pic.jpg"
    # Nested YYYY/MM date-bucketing: no EXIF/original_date for a bare "hello
    # world" blob, so it falls back to "now" — parent is a 2-digit month
    # under a 4-digit year under Photos/.
    assert result.path.parent.name.split("-p")[0].isdigit()
    assert len(result.path.parent.name.split("-p")[0]) == 2
    assert result.path.parent.parent.name.isdigit()
    assert len(result.path.parent.parent.name) == 4
    assert result.path.parent.parent.parent.name == "Photos"
    assert result.path.exists()
    assert not result.path.with_name(result.path.name + ".uploading").exists()


@pytest.mark.asyncio
async def test_ingest_dedup_hit_on_second_identical_upload(client):
    """The same content uploaded twice into the same scope dedup-hits the second time."""
    dest = Destination(scope=Scope.FAMILY, mode=IngestMode.SORTED)

    first = await ingest(_chunks(b"same bytes"), filename="a.jpg", dest=dest)
    assert first.dedup_hit is False

    second = await ingest(_chunks(b"same bytes"), filename="b.jpg", dest=dest)
    assert second.dedup_hit is True
    assert second.sha256 == first.sha256
    # The second (duplicate) temp file must not be left behind.
    assert not (first.path.parent / "b.jpg").exists()
    assert not (first.path.parent / "b.jpg.uploading").exists()
    # A dedup-hit result.path must be absolute like every other return site —
    # a caller doing result.path.relative_to(nas_root) (the real /upload
    # route's JSON response) must not crash on a dedup hit.
    assert second.path.is_absolute()
    assert second.path == first.path
    from app.config import settings
    second.path.relative_to(settings.nas_root.resolve())  # raises if not absolute-under-root


@pytest.mark.asyncio
async def test_ingest_same_content_different_scope_not_deduped(client):
    """Dedup is per-(scope, hash) — the same file in two different scopes is not collapsed
    (a family copy existing does not suppress a personal copy of the same content)."""
    family_dest = Destination(scope=Scope.FAMILY, mode=IngestMode.SORTED)
    personal_dest = Destination(scope=Scope.PERSONAL, owner="alice", mode=IngestMode.SORTED)

    family_result = await ingest(_chunks(b"cross-scope bytes"), filename="x.jpg", dest=family_dest)
    personal_result = await ingest(_chunks(b"cross-scope bytes"), filename="x.jpg", dest=personal_dest)

    assert family_result.dedup_hit is False
    assert personal_result.dedup_hit is False
    assert family_result.path.exists()
    assert personal_result.path.exists()


@pytest.mark.asyncio
async def test_dedup_invalidate_allows_reingest_after_delete(client):
    """Re-uploading identical content after the original was deleted must be treated
    as new, not silently skipped as an already-known duplicate — a real bug found via
    live end-to-end testing: deleting a file never cleared its dedup hash, so a later
    re-sync of the same bytes reported success while writing nothing.

    Since the dedup migration off the JSON ingest_hashes store, dedup reads live
    (non-deleted) media_index entries directly — there is no separate hash store to
    invalidate anymore. Marking the entry deleted in media_index (exactly what
    file_routes._soft_delete_resolved does on every delete) is sufficient on its own."""
    from app import media_index

    dest = Destination(scope=Scope.FAMILY, mode=IngestMode.SORTED)

    first = await ingest(_chunks(b"deleted then resynced"), filename="a.jpg", dest=dest)
    assert first.dedup_hit is False
    from app.config import settings
    rel_path = "/" + str(first.path.relative_to(settings.nas_root.resolve())).replace("\\", "/")

    # Simulate the delete route's cleanup (file_routes._soft_delete_resolved calls this).
    await media_index.mark_entry_deleted_by_rel_path(rel_path)

    second = await ingest(_chunks(b"deleted then resynced"), filename="b.jpg", dest=dest)
    assert second.dedup_hit is False
    assert second.path.exists()


@pytest.mark.asyncio
async def test_ingest_personal_dedup_is_owner_scoped(client):
    """Personal-scope dedup must be per-owner — one member's private upload of some
    bytes must never suppress (or leak the path of) another member's upload of the
    same bytes. The old JSON ingest_hashes store keyed dedup on scope alone and got
    this wrong; media_index-backed dedup scopes personal lookups by owner too."""
    alice_dest = Destination(scope=Scope.PERSONAL, owner="alice", mode=IngestMode.SORTED)
    bob_dest = Destination(scope=Scope.PERSONAL, owner="bob", mode=IngestMode.SORTED)

    alice_result = await ingest(_chunks(b"shared bytes, private copies"), filename="p.jpg", dest=alice_dest)
    bob_result = await ingest(_chunks(b"shared bytes, private copies"), filename="p.jpg", dest=bob_dest)

    assert alice_result.dedup_hit is False
    assert bob_result.dedup_hit is False
    assert alice_result.path.exists()
    assert bob_result.path.exists()
    assert alice_result.path != bob_result.path


# ─── bucket allocator: the structural ≤500-items-per-directory guarantee ──────

@pytest.mark.asyncio
async def test_bucket_allocator_splits_after_limit(client):
    """The core hard guarantee this redesign exists for: once a physical month
    bucket reaches BUCKET_LIMIT files, the allocator must open a new "-bN" part
    directory rather than letting the original directory grow past the limit —
    a structural (transactional-counter) guarantee, not the old racy scandir
    threshold check."""
    from app import media_index

    bucket_key = "family/Photos/2024/01"
    # Fill part 1 to exactly the limit.
    for _ in range(media_index.BUCKET_LIMIT):
        media_index.allocate_bucket_sync(bucket_key)

    # The next allocation must overflow into part 2.
    leaf = media_index.allocate_bucket_sync(bucket_key)
    assert leaf == "01-b2"


@pytest.mark.asyncio
async def test_bucket_allocator_is_per_key(client):
    """Two different bucket_keys (e.g. different months, or different
    scope/category) must allocate independently — filling one must not affect
    another."""
    from app import media_index

    for _ in range(5):
        media_index.allocate_bucket_sync("family/Photos/2024/02")

    # A fresh key starts at part 1 regardless of another key's fill level.
    leaf = media_index.allocate_bucket_sync("family/Videos/2024/02")
    assert leaf == "02"


@pytest.mark.asyncio
async def test_bucket_allocator_prefers_lowest_part_with_room(client):
    """Regression pin (found live, device smoke test 2026-07-13): when MULTIPLE
    parts of the same bucket simultaneously have room — e.g. after a bucket-repair
    pass resets a higher part's drifted count back down to its real, low value —
    the allocator must fill the LOWEST-numbered part first, not scatter new
    allocations into a higher part just because it also has room. The original
    query (ORDER BY part DESC) picked the highest part with room instead, which
    kept using a mostly-empty part-2 while part-1 sat far from full."""
    from app import media_index

    bucket_key = "family/Photos/2026/07"
    with media_index._get_conn() as conn:
        conn.execute(
            "INSERT INTO buckets (bucket_key, part, count) VALUES (?, 1, 2)", (bucket_key,),
        )
        conn.execute(
            "INSERT INTO buckets (bucket_key, part, count) VALUES (?, 2, 0)", (bucket_key,),
        )
        conn.commit()

    leaf = media_index.allocate_bucket_sync(bucket_key)
    assert leaf == "07", "must fill part 1 (has room) before touching part 2"

    with media_index._get_conn() as conn:
        row = conn.execute(
            "SELECT count FROM buckets WHERE bucket_key = ? AND part = 1", (bucket_key,),
        ).fetchone()
    assert row["count"] == 3


@pytest.mark.asyncio
async def test_ingest_stall_timeout_cleans_up_temp(client):
    """A chunk source that goes silent longer than stall_timeout_s raises IngestStallError
    and leaves no partial .uploading file behind."""
    dest = Destination(scope=Scope.FAMILY, mode=IngestMode.SORTED)

    with pytest.raises(IngestStallError):
        await ingest(
            _stalling_chunks(b"first chunk"), filename="stalled.jpg", dest=dest,
            stall_timeout_s=1,
        )

    from app.config import settings
    leftovers = list((settings.family_path / "Photos").glob("stalled.jpg*")) if (settings.family_path / "Photos").exists() else []
    assert leftovers == []


@pytest.mark.asyncio
async def test_ingest_oversize_rejected_and_cleaned_up(client, monkeypatch):
    """A stream exceeding max_upload_bytes is rejected and its temp file removed."""
    from app.config import settings
    monkeypatch.setattr(settings, "max_upload_bytes", 5)

    dest = Destination(scope=Scope.FAMILY, mode=IngestMode.SORTED)
    with pytest.raises(IngestSizeError):
        await ingest(_chunks(b"way more than five bytes"), filename="big.jpg", dest=dest)

    leftovers = list((settings.family_path / "Photos").glob("big.jpg*")) if (settings.family_path / "Photos").exists() else []
    assert leftovers == []


@pytest.mark.asyncio
async def test_ingest_preflight_rejects_when_size_hint_exceeds_free_space(client, monkeypatch):
    """SEC/reliability finding (2026-07-16 audit): a full drive on a normal upload
    previously had no preflight at all. When the caller already knows the size, reject
    before writing a single byte."""
    import shutil as shutil_mod
    from app import ingest as ingest_mod

    fake_usage = type("Usage", (), {"total": 10**9, "used": 10**9 - 10, "free": 10})()
    monkeypatch.setattr(ingest_mod.shutil, "disk_usage", lambda *a, **k: fake_usage)

    dest = Destination(scope=Scope.FAMILY, mode=IngestMode.SORTED)
    with pytest.raises(IngestDiskFullError):
        await ingest(_chunks(b"x"), filename="toobig.jpg", dest=dest, size_hint=10_000)

    from app.config import settings
    leftovers = list((settings.family_path / "Photos").glob("toobig.jpg*")) if (settings.family_path / "Photos").exists() else []
    assert leftovers == []


@pytest.mark.asyncio
async def test_ingest_mid_write_disk_full_raises_clear_error_and_cleans_up(client, monkeypatch):
    """No size_hint available (the common case) -- the drive fills mid-write instead.
    Must surface as IngestDiskFullError (not a raw, unhelpful OSError) and still clean
    up the partial .uploading temp file, exactly like the stall/oversize paths above."""
    import errno as errno_mod
    from app import ingest as ingest_mod

    real_open = open

    class _FullDiskFile:
        def __init__(self, real_fd):
            self._real_fd = real_fd

        def write(self, data):
            raise OSError(errno_mod.ENOSPC, "No space left on device")

        def flush(self):
            pass

        def close(self):
            self._real_fd.close()

        def fileno(self):
            return self._real_fd.fileno()

    def fake_open(path, mode, buffering=-1):
        return _FullDiskFile(real_open(path, mode, buffering=buffering))

    monkeypatch.setattr(ingest_mod, "open", fake_open, raising=False)

    dest = Destination(scope=Scope.FAMILY, mode=IngestMode.SORTED)
    with pytest.raises(IngestDiskFullError):
        await ingest(_chunks(b"some bytes"), filename="fillsdisk.jpg", dest=dest)

    from app.config import settings
    leftovers = list((settings.family_path / "Photos").glob("fillsdisk.jpg*")) if (settings.family_path / "Photos").exists() else []
    assert leftovers == [], "a disk-full write must clean up its partial temp file, same as stall/oversize"


@pytest.mark.asyncio
async def test_ingest_personal_scope_requires_owner_auth(client):
    """A non-admin, non-owner user is rejected from writing into another user's personal scope."""
    from app import store

    await store.add_user("alice", "1111")
    bob = await store.add_user("bob", "2222")
    bob_user_claims = {"sub": bob["id"], "type": "user"}

    dest = Destination(scope=Scope.PERSONAL, owner="alice", mode=IngestMode.SORTED)

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        await ingest(_chunks(b"private"), filename="secret.jpg", dest=dest, user=bob_user_claims)
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_ingest_mirror_mode_no_sort(client):
    """MIRROR mode with an explicit subpath writes verbatim, no extension-based sort."""
    dest = Destination(scope=Scope.FAMILY, subpath="Raw", mode=IngestMode.MIRROR)
    result = await ingest(_chunks(b"raw bytes"), filename="clip.mp4", dest=dest)

    assert result.sorted_to is None
    assert result.path.parent.name == "Raw"
    assert result.path.name == "clip.mp4"


@pytest.mark.asyncio
async def test_ingest_raw_dir_bypasses_scope_derivation(client, tmp_path):
    """A Destination built from an already-resolved arbitrary directory (raw_dir) writes
    exactly there — this is what explicit-path app uploads use, and must not silently
    redirect uploads to a scope-derived path."""
    from app.config import settings

    arbitrary_dir = settings.nas_root / "shared"
    dest = Destination(scope=Scope.FAMILY, raw_dir=arbitrary_dir, mode=IngestMode.MIRROR)
    result = await ingest(_chunks(b"arbitrary"), filename="note.txt", dest=dest)

    assert result.path.parent == arbitrary_dir


# ─── capture-date bucketing ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ingest_buckets_by_original_date_when_no_exif(client):
    """No embedded EXIF on a plain byte blob — caller-supplied original_date
    (e.g. the phone's real capture date on a sync/direct upload) decides the
    YYYY/MM bucket."""
    from datetime import datetime, timezone

    dest = Destination(scope=Scope.FAMILY, mode=IngestMode.SORTED)
    original_date = datetime(2023, 11, 5, tzinfo=timezone.utc).timestamp()
    result = await ingest(
        _chunks(b"november photo"), filename="pic.jpg", dest=dest, original_date=original_date,
    )

    assert result.path.parent.name == "11"
    assert result.path.parent.parent.name == "2023"


@pytest.mark.asyncio
async def test_ingest_buckets_by_filename_date_when_no_exif_or_original_date(client):
    """Neither EXIF nor a caller-supplied original_date — falls back to a
    YYYYMMDD pattern in the filename (WhatsApp/screenshot convention)."""
    dest = Destination(scope=Scope.FAMILY, mode=IngestMode.SORTED)
    result = await ingest(
        _chunks(b"whatsapp photo"), filename="IMG-20220304-WA0007.jpg", dest=dest,
    )

    assert result.path.parent.name == "03"
    assert result.path.parent.parent.name == "2022"


@pytest.mark.asyncio
async def test_ingest_records_capture_date_matching_physical_bucket(client):
    """The capture epoch used to bucket physically is the exact same value
    recorded in media_index — the two must never disagree."""
    from datetime import datetime, timezone
    from app import media_index

    dest = Destination(scope=Scope.FAMILY, mode=IngestMode.SORTED, owner=None)
    original_date = datetime(2025, 8, 1, tzinfo=timezone.utc).timestamp()
    result = await ingest(
        _chunks(b"august photo"), filename="aug.jpg", dest=dest, original_date=original_date,
    )

    rows = await media_index.query_entries(scope="family", limit=10)
    matching = [r for r in rows if r["filename"] == "aug.jpg"]
    assert len(matching) == 1
    assert matching[0]["capture_date"] == int(original_date)
    assert result.path.parent.name == "08"
    assert result.path.parent.parent.name == "2025"
