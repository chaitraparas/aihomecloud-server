"""
Tests for app/media_reconciler.py — the nightly out-of-band reconcile pass
(prune deleted files, incrementally re-index changed/new files, repair the
bucket allocator's counters against reality).
"""

import asyncio

import pytest


@pytest.mark.asyncio
async def test_reconcile_discovers_out_of_band_file(client):
    """A file dropped directly onto disk (never through ingest()) is discovered
    and recorded by the reconciler — the whole reason media_index needs a
    reconcile pass at all, since it's normally only written at ingest time."""
    from app import media_index
    from app.config import settings
    from app.media_reconciler import reconcile_once

    photos_dir = settings.family_path / "Photos" / "2025" / "01"
    photos_dir.mkdir(parents=True)
    (photos_dir / "dropped.jpg").write_bytes(b"\xff\xd8\xff" + b"\x00" * 512)

    result = await reconcile_once()
    assert result["indexed"] == 1
    assert result["scanned"] == 1

    entries = await media_index.query_entries(scope="family")
    assert len(entries) == 1
    assert entries[0]["filename"] == "dropped.jpg"
    assert entries[0]["category"] == "Photos"


@pytest.mark.asyncio
async def test_reconcile_prunes_entries_for_deleted_files(client):
    """A file recorded via ingest(), then removed directly from disk (bypassing
    the app entirely, e.g. over SMB) — the reconciler marks its entry deleted."""
    from app import media_index
    from app.ingest import Destination, IngestMode, Scope, ingest

    async def _chunks(data: bytes):
        yield data

    dest = Destination(scope=Scope.FAMILY, mode=IngestMode.SORTED)
    result = await ingest(_chunks(b"will be removed"), filename="gone.jpg", dest=dest)
    assert result.path.exists()

    entries = await media_index.query_entries(scope="family")
    assert len(entries) == 1

    result.path.unlink()  # simulate an out-of-band delete (e.g. raw SMB)

    from app.media_reconciler import reconcile_once
    reconcile_result = await reconcile_once()
    assert reconcile_result["pruned"] == 1

    entries = await media_index.query_entries(scope="family")
    assert entries == []


@pytest.mark.asyncio
async def test_reconcile_skips_unchanged_files_on_second_pass(client, monkeypatch):
    """A file whose (rel_path, size, mtime) already matches what's recorded is not
    re-hashed on a second reconcile pass — the incremental-skip contract this
    module exists for, since a full rehash isn't viable nightly on this hardware."""
    from app.config import settings
    from app.media_reconciler import reconcile_once
    import app.media_reconciler as media_reconciler_module

    photos_dir = settings.family_path / "Photos" / "2025" / "02"
    photos_dir.mkdir(parents=True)
    (photos_dir / "stable.jpg").write_bytes(b"\xff\xd8\xff" + b"\x00" * 512)

    first = await reconcile_once()
    assert first["indexed"] == 1

    hash_calls = []
    real_sha256_of = media_reconciler_module._sha256_of

    def _counting_sha256_of(path):
        hash_calls.append(path)
        return real_sha256_of(path)

    monkeypatch.setattr(media_reconciler_module, "_sha256_of", _counting_sha256_of)

    second = await reconcile_once()
    assert second["indexed"] == 0
    assert second["skipped"] == 1
    assert hash_calls == []


@pytest.mark.asyncio
async def test_reconcile_reindexes_changed_file(client):
    """A file whose content actually changed (different size/mtime) IS re-hashed
    and re-recorded on the next pass — the incremental skip must not silently
    ignore genuine changes, only genuinely-unchanged files."""
    import os
    import time
    from app import media_index
    from app.config import settings
    from app.media_reconciler import reconcile_once

    photos_dir = settings.family_path / "Photos" / "2025" / "03"
    photos_dir.mkdir(parents=True)
    f = photos_dir / "changing.jpg"
    f.write_bytes(b"\xff\xd8\xff" + b"\x00" * 512)

    first = await reconcile_once()
    assert first["indexed"] == 1

    # Change content + bump mtime so the (size, mtime) signature actually differs.
    f.write_bytes(b"\xff\xd8\xff" + b"\x11" * 900)
    os.utime(f, (time.time() + 5, time.time() + 5))

    second = await reconcile_once()
    assert second["indexed"] == 1
    assert second["skipped"] == 0

    entries = await media_index.query_entries(scope="family")
    assert len(entries) == 1
    assert entries[0]["size_bytes"] == 903


@pytest.mark.asyncio
async def test_reconcile_repairs_bucket_counter_drift(client):
    """A bucket allocator counter that's drifted from reality (e.g. a crash between
    allocation and file write) is corrected to match a real directory scan."""
    from app import media_index
    from app.config import settings
    from app.media_reconciler import reconcile_once

    bucket_key = "family/Photos/2025/04"
    # Allocate 3 slots in the counter...
    for _ in range(3):
        media_index.allocate_bucket_sync(bucket_key)

    # ...but only physically write 1 file (simulating 2 crashed mid-ingest).
    photos_dir = settings.family_path / "Photos" / "2025" / "04"
    photos_dir.mkdir(parents=True)
    (photos_dir / "only_one.jpg").write_bytes(b"x")

    result = await reconcile_once()
    assert result["buckets_repaired"] == 1

    with media_index._get_conn() as conn:
        row = conn.execute(
            "SELECT count FROM buckets WHERE bucket_key = ? AND part = 1", (bucket_key,),
        ).fetchone()
    assert row["count"] == 1


@pytest.mark.asyncio
async def test_reindex_route_runs_media_reconcile_too(authenticated_client):
    """POST /files/reindex reconciles BOTH indexes now, not just documents/OCR —
    the job result must carry the media reconcile's stats too."""
    from app import document_index
    from app.config import settings

    # The client fixture only initializes media_index's DB — /reindex also touches
    # document_index (pre-existing gap, unrelated to this test's actual subject).
    await document_index.init_db()

    photos_dir = settings.family_path / "Photos" / "2025" / "05"
    photos_dir.mkdir(parents=True)
    (photos_dir / "will_be_found.jpg").write_bytes(b"\xff\xd8\xff" + b"\x00" * 512)

    resp = await authenticated_client.post("/api/v1/files/reindex")
    assert resp.status_code == 202
    job_id = resp.json()["jobId"]

    for _ in range(50):
        status_resp = await authenticated_client.get(f"/api/v1/jobs/{job_id}")
        body = status_resp.json()
        if body["status"] in ("completed", "failed"):
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("reindex job did not complete in time")

    assert body["status"] == "completed", body
    result = body["result"]
    assert result["mediaIndexed"] == 1
    assert "mediaPruned" in result
    assert "bucketsRepaired" in result
