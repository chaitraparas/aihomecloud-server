"""
Duplicate Scanner Tests — unit tests for duplicate detection and deletion endpoints.
"""

import hashlib
import pytest
from pathlib import Path
from httpx import AsyncClient


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


# ---------------------------------------------------------------------------
# Unit tests — _scan_sync (blocking)
# ---------------------------------------------------------------------------

def test_scanner_finds_exact_duplicates(tmp_path: Path):
    """Two identical files (>= 10 KB) should appear as one duplicate set."""
    from app.duplicate_scanner import _scan_sync, _MIN_SIZE_BYTES

    content = b"X" * _MIN_SIZE_BYTES  # exactly at the threshold
    _write(tmp_path / "a" / "file.bin", content)
    _write(tmp_path / "b" / "file.bin", content)

    results = _scan_sync(tmp_path)
    assert len(results) == 1
    assert results[0]["hash"] == _sha256(content)
    assert len(results[0]["copies"]) == 2


def test_scanner_skips_small_files(tmp_path: Path):
    """Files below 10 KB must be ignored."""
    from app.duplicate_scanner import _scan_sync, _MIN_SIZE_BYTES

    small = b"Z" * (_MIN_SIZE_BYTES - 1)
    _write(tmp_path / "a" / "tiny.bin", small)
    _write(tmp_path / "b" / "tiny.bin", small)

    results = _scan_sync(tmp_path)
    assert results == []


def test_scanner_skips_trash(tmp_path: Path):
    """Files inside .ahc_trash must not be scanned."""
    from app.duplicate_scanner import _scan_sync, _MIN_SIZE_BYTES

    content = b"T" * _MIN_SIZE_BYTES
    _write(tmp_path / ".ahc_trash" / "file.bin", content)
    _write(tmp_path / "keep" / "file.bin", content)

    results = _scan_sync(tmp_path)
    # Only the file outside trash counts — no duplicate set
    assert results == []


def test_scanner_cross_folder(tmp_path: Path):
    """Duplicates across different user folders are detected."""
    from app.duplicate_scanner import _scan_sync, _MIN_SIZE_BYTES

    content = b"C" * (_MIN_SIZE_BYTES * 2)
    _write(tmp_path / "personal" / "Alice" / "photo.jpg", content)
    _write(tmp_path / "personal" / "Bob" / "photo.jpg", content)
    _write(tmp_path / "family" / "photo.jpg", content)

    results = _scan_sync(tmp_path)
    assert len(results) == 1
    assert len(results[0]["copies"]) == 3


@pytest.mark.asyncio
async def test_scan_respects_exact_whitelist(tmp_path: Path, monkeypatch):
    """A hash in duplicate_exact_whitelist must be excluded from scan results.

    Otherwise an intentional duplicate (kept via /keep, or shared to family) has no
    way to be permanently silenced and resurfaces on every nightly rescan.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("AHC_DATA_DIR", str(data_dir))
    from app.config import settings
    from app import store
    from app.duplicate_scanner import DuplicateScanner, _MIN_SIZE_BYTES

    settings.data_dir = data_dir
    settings.nas_root = tmp_path / "nas"
    store._cache.clear()

    content = b"W" * _MIN_SIZE_BYTES
    _write(settings.personal_path / "alice" / "photo.jpg", content)
    _write(settings.family_path / "photo.jpg", content)

    scanner = DuplicateScanner()
    exact, _ = await scanner._scan_nas_for_duplicates()
    assert len(exact) == 1
    full_hash = exact[0]["hash"]

    await store.set_value("duplicate_exact_whitelist", [full_hash])
    exact2, _ = await scanner._scan_nas_for_duplicates()
    assert exact2 == []


# ---------------------------------------------------------------------------
# Integration tests — HTTP endpoints
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delete_duplicate_endpoint(
    client: AsyncClient,
    admin_token: str,
    tmp_path: Path,
):
    """DELETE /api/v1/backup/duplicates/<path> removes the file and returns 200."""
    from app import store
    from app.config import settings

    # Place a real file inside nas_root so the path-safety check passes
    target = settings.nas_root / "shared" / "dup_test_file.bin"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"D" * 1024)

    # Pre-populate store with a matching duplicate entry
    path_str = str(target)
    await store.set_value("duplicate_scan_results", [
        {
            "hash": "aabbccdd" * 8,
            "filename": "dup_test_file.bin",
            "sizeBytes": 1024,
            "copies": [
                {"path": path_str, "owner": "shared"},
                {"path": path_str + ".copy", "owner": "shared"},
            ],
        }
    ])

    import urllib.parse
    encoded = urllib.parse.quote(path_str, safe="")
    res = await client.delete(
        f"/api/v1/backup/duplicates/{encoded}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 200
    assert res.json()["deleted"] == path_str
    assert not target.exists()

    # Stored results should no longer contain a set with 2 copies
    updated = await store.get_value("duplicate_scan_results", default=[])
    for entry in updated:
        assert len(entry.get("copies", [])) >= 2


@pytest.mark.asyncio
async def test_delete_duplicate_endpoint_soft_deletes_to_trash(
    client: AsyncClient,
    admin_token: str,
):
    """
    M1 regression: deleting a duplicate must be recoverable via trash, not a permanent
    resolved.unlink(). Before the fix this route hard-deleted directly, bypassing the trash
    every other delete path uses.
    """
    from app import store
    from app.config import settings

    target = settings.nas_root / "shared" / "dup_test_recoverable.bin"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"D" * 1024)
    path_str = str(target)

    await store.set_value("duplicate_scan_results", [
        {
            "hash": "eeff0011" * 8,
            "filename": "dup_test_recoverable.bin",
            "sizeBytes": 1024,
            "copies": [
                {"path": path_str, "owner": "shared"},
                {"path": path_str + ".copy", "owner": "shared"},
            ],
        }
    ])

    import urllib.parse
    encoded = urllib.parse.quote(path_str, safe="")
    res = await client.delete(
        f"/api/v1/backup/duplicates/{encoded}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 200
    assert not target.exists(), "file must no longer be at its original location"

    trash_items = await store.get_trash_items()
    matches = [i for i in trash_items if i["filename"] == "dup_test_recoverable.bin"]
    assert len(matches) == 1, "deleted duplicate must appear in trash, recoverable"
    assert Path(matches[0]["trashPath"]).exists(), "the actual bytes must have moved into trash, not vanished"


@pytest.mark.asyncio
async def test_scan_endpoint_requires_admin(
    client: AsyncClient,
    member_token: str,
):
    """POST /api/v1/backup/duplicates/scan must reject non-admin users with 403."""
    res = await client.post(
        "/api/v1/backup/duplicates/scan",
        headers={"Authorization": f"Bearer {member_token}"},
    )
    assert res.status_code == 403
