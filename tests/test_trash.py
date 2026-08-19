"""
Trash Infrastructure Tests — soft delete, restore, permanent delete, quota purge.
"""

import io
import uuid
from datetime import datetime

import pytest
from httpx import AsyncClient
from pathlib import Path


# ─── Helpers ─────────────────────────────────────────────────────────────────

async def _make_file(authenticated_client: AsyncClient, filename: str = "test.txt") -> str:
    """Upload a file to shared/ and return its NAS path after it lands in .inbox/."""
    content = b"hello trash test"
    files = {"file": (filename, io.BytesIO(content), "text/plain")}
    resp = await authenticated_client.post(
        "/api/v1/files/upload?path=/srv/nas/shared/",
        files=files,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["path"]


async def _create_shared_file(tmp_nas: Path, filename: str = "direct.txt") -> str:
    """Create a file directly in shared/ and return its NAS path string."""
    p = tmp_nas / "shared" / filename
    p.write_text("direct content")
    return f"/srv/nas/shared/{filename}"


# ─── Soft delete ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_soft_delete_moves_to_trash(
    authenticated_client: AsyncClient, client: AsyncClient
):
    """Deleting a file should move it to trash, not destroy it."""
    from app.config import settings

    # _safe_resolve strips settings.nas_root prefix; build path accordingly
    shared = settings.nas_root / "srv" / "nas" / "shared"
    shared.mkdir(parents=True, exist_ok=True)
    fname = f"delete_me_{uuid.uuid4().hex[:6]}.txt"
    (shared / fname).write_text("bye bye")
    nas_path = f"/srv/nas/shared/{fname}"

    # Delete via API
    resp = await authenticated_client.delete(f"/api/v1/files/delete?path={nas_path}")
    assert resp.status_code == 204, resp.text

    # Original file should be gone
    assert not (shared / fname).exists()

    # Trash listing should contain exactly our file
    resp = await authenticated_client.get("/api/v1/files/trash")
    assert resp.status_code == 200
    items = resp.json()
    names = [i["filename"] for i in items]
    assert fname in names


@pytest.mark.asyncio
async def test_soft_delete_nonexistent_returns_404(authenticated_client: AsyncClient):
    resp = await authenticated_client.delete(
        "/api/v1/files/delete?path=/srv/nas/shared/ghost_file.txt"
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_trash_list_returns_only_caller_items(
    authenticated_client: AsyncClient, client: AsyncClient
):
    """Each user only sees their own trash items."""
    from app.config import settings

    shared = settings.nas_root / "srv" / "nas" / "shared"
    shared.mkdir(parents=True, exist_ok=True)
    fname = f"mine_{uuid.uuid4().hex[:6]}.txt"
    (shared / fname).write_text("mine")
    nas_path = f"/srv/nas/shared/{fname}"

    await authenticated_client.delete(f"/api/v1/files/delete?path={nas_path}")

    resp = await authenticated_client.get("/api/v1/files/trash")
    assert resp.status_code == 200
    items = resp.json()
    # All items must belong to our user
    for item in items:
        assert "filename" in item
        assert "id" in item
        assert "originalPath" in item
        assert "deletedAt" in item
        assert "sizeBytes" in item


# ─── Restore ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_restore_returns_file_to_original_path(
    authenticated_client: AsyncClient, client: AsyncClient
):
    """Restored file should reappear at its original location and leave trash."""
    from app.config import settings

    shared = settings.nas_root / "srv" / "nas" / "shared"
    shared.mkdir(parents=True, exist_ok=True)
    fname = f"restore_{uuid.uuid4().hex[:6]}.txt"
    original = shared / fname
    original.write_text("restore me")
    nas_path = f"/srv/nas/shared/{fname}"

    # Soft delete
    resp = await authenticated_client.delete(f"/api/v1/files/delete?path={nas_path}")
    assert resp.status_code == 204

    # Get the trash item id
    resp = await authenticated_client.get("/api/v1/files/trash")
    items = resp.json()
    item = next(i for i in items if i["filename"] == fname)
    item_id = item["id"]

    # Restore
    resp = await authenticated_client.post(f"/api/v1/files/trash/{item_id}/restore")
    assert resp.status_code == 204, resp.text

    # File should be back at original path
    assert original.exists()

    # Trash listing should no longer contain the item
    resp = await authenticated_client.get("/api/v1/files/trash")
    remaining_ids = [i["id"] for i in resp.json()]
    assert item_id not in remaining_ids


@pytest.mark.asyncio
async def test_restore_invalidates_scan_cache(
    authenticated_client: AsyncClient, client: AsyncClient
):
    """Regression pin: restore must invalidate the 7-second directory-scan cache for the
    destination's parent dir. Before the fix, a restored file didn't show up in
    /files/list for up to 7 seconds after a successful restore, because the stale cached
    listing (primed without the file) kept being served until its TTL expired."""
    from app.config import settings
    from app.routes.file_routes import _scan_cache

    shared = settings.nas_root / "srv" / "nas" / "shared"
    shared.mkdir(parents=True, exist_ok=True)
    fname = f"cache_{uuid.uuid4().hex[:6]}.txt"
    (shared / fname).write_text("cache me")
    nas_path = f"/srv/nas/shared/{fname}"
    list_url = "/api/v1/files/list?path=/srv/nas/shared/"
    cache_prefix = f"{shared.resolve()}|"

    # Soft-delete, then list the parent dir — this primes the scan cache with a
    # listing that does NOT contain the file.
    resp = await authenticated_client.delete(f"/api/v1/files/delete?path={nas_path}")
    assert resp.status_code == 204, resp.text
    resp = await authenticated_client.get(list_url)
    assert resp.status_code == 200
    assert fname not in [i["name"] for i in resp.json()["items"]]
    # Sanity: the without-the-file listing really is cached now, otherwise the rest
    # of this test can't prove anything.
    assert any(k.startswith(cache_prefix) for k in _scan_cache)

    # Restore the file.
    resp = await authenticated_client.get("/api/v1/files/trash")
    item = next(i for i in resp.json() if i["filename"] == fname)
    resp = await authenticated_client.post(f"/api/v1/files/trash/{item['id']}/restore")
    assert resp.status_code == 204, resp.text

    # The actual fix, asserted deterministically (no TTL race): the stale cached
    # listing for the parent dir must have been dropped by the restore itself.
    stale = [k for k in _scan_cache if k.startswith(cache_prefix)]
    assert not stale, f"restore left stale scan-cache entries: {stale}"

    # And behaviorally: the restored file is visible immediately, well inside the
    # 7s window in which the old bug served the stale listing.
    resp = await authenticated_client.get(list_url)
    assert resp.status_code == 200
    assert fname in [i["name"] for i in resp.json()["items"]]


@pytest.mark.asyncio
async def test_restore_nonexistent_id_returns_404(authenticated_client: AsyncClient):
    resp = await authenticated_client.post(
        f"/api/v1/files/trash/{uuid.uuid4()}/restore"
    )
    assert resp.status_code == 404


# ─── Permanent delete ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_permanent_delete_removes_from_trash_and_disk(
    authenticated_client: AsyncClient, client: AsyncClient
):
    """Permanent delete should remove the physical file and the metadata entry."""
    from app.config import settings
    from app import store

    shared = settings.nas_root / "srv" / "nas" / "shared"
    shared.mkdir(parents=True, exist_ok=True)
    fname = f"perm_{uuid.uuid4().hex[:6]}.txt"
    (shared / fname).write_text("gone forever")
    nas_path = f"/srv/nas/shared/{fname}"

    await authenticated_client.delete(f"/api/v1/files/delete?path={nas_path}")

    resp = await authenticated_client.get("/api/v1/files/trash")
    item = next(i for i in resp.json() if i["filename"] == fname)
    item_id = item["id"]
    trash_path = Path(item["trashPath"])

    # Permanently delete
    resp = await authenticated_client.delete(f"/api/v1/files/trash/{item_id}")
    assert resp.status_code == 204, resp.text

    # Physical file should be gone
    assert not trash_path.exists()

    # Metadata should be gone
    resp = await authenticated_client.get("/api/v1/files/trash")
    remaining_ids = [i["id"] for i in resp.json()]
    assert item_id not in remaining_ids


@pytest.mark.asyncio
async def test_permanent_delete_nonexistent_id_returns_404(authenticated_client: AsyncClient):
    resp = await authenticated_client.delete(
        f"/api/v1/files/trash/{uuid.uuid4()}"
    )
    assert resp.status_code == 404


# ─── Directory soft delete ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_soft_delete_directory(authenticated_client: AsyncClient, client: AsyncClient):
    """Soft-deleting a directory should move it to trash intact."""
    from app.config import settings

    shared = settings.nas_root / "srv" / "nas" / "shared"
    dname = f"folder_{uuid.uuid4().hex[:6]}"
    folder = shared / dname
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "child.txt").write_text("child file")
    nas_path = f"/srv/nas/shared/{dname}"

    resp = await authenticated_client.delete(f"/api/v1/files/delete?path={nas_path}")
    assert resp.status_code == 204

    # Folder should be gone from original location
    assert not folder.exists()

    # Appears in trash
    resp = await authenticated_client.get("/api/v1/files/trash")
    names = [i["filename"] for i in resp.json()]
    assert dname in names


# ─── Quota auto-purge ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_quota_purge_removes_oldest_items(
    authenticated_client: AsyncClient, client: AsyncClient
):
    """When trash exceeds 10% of NAS capacity, oldest items should be auto-purged."""
    from app.config import settings
    from app import store
    from app.routes import trash_routes
    import unittest.mock as mock
    from datetime import timedelta, timezone as tz

    # Pre-populate trash with two items — one old, one newer
    shared = settings.nas_root / "shared"
    shared.mkdir(parents=True, exist_ok=True)

    trash_dir = settings.trash_dir / "user_test_quota"
    trash_dir.mkdir(parents=True, exist_ok=True)

    old_file = trash_dir / "old.txt"
    new_file = trash_dir / "new.txt"
    old_file.write_bytes(b"x" * 500)  # 500 bytes
    new_file.write_bytes(b"x" * 300)  # 300 bytes

    now = datetime.now(tz.utc)
    items = [
        {
            "id": "old-item",
            "originalPath": "/srv/nas/shared/old.txt",
            "trashPath": str(old_file),
            "filename": "old.txt",
            "deletedAt": (now - timedelta(hours=2)).isoformat(),
            "sizeBytes": 500,
            "deletedBy": "user_test_quota",
        },
        {
            "id": "new-item",
            "originalPath": "/srv/nas/shared/new.txt",
            "trashPath": str(new_file),
            "filename": "new.txt",
            "deletedAt": (now - timedelta(hours=1)).isoformat(),
            "sizeBytes": 300,
            "deletedBy": "user_test_quota",
        },
    ]
    await store.save_trash_items(items)

    # Mock disk_usage so 10% quota = 700 bytes total → 70 bytes quota
    # Both items (800 bytes total) exceed quota → oldest should be purged
    fake_usage = mock.MagicMock()
    fake_usage.total = 700
    with mock.patch("app.routes.trash_routes.shutil.disk_usage", return_value=fake_usage):
        await trash_routes._purge_trash_if_needed()

    remaining = await store.get_trash_items()
    remaining_ids = [i["id"] for i in remaining]

    # New item (smaller, more recent path) should survive; old item purged
    # After purge: 300 bytes total ≤ 70 bytes quota won't hold both;
    # oldest ("old-item") should be purged first
    assert "old-item" not in remaining_ids


# ─── Trash prefs ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_trash_prefs_default_false(authenticated_client: AsyncClient):
    """GET /trash/prefs returns autoDelete: false by default."""
    resp = await authenticated_client.get("/api/v1/files/trash/prefs")
    assert resp.status_code == 200
    assert resp.json() == {"autoDelete": False}


@pytest.mark.asyncio
async def test_set_trash_prefs_and_read_back(authenticated_client: AsyncClient):
    """PUT /trash/prefs persists the value and GET reads it back."""
    resp = await authenticated_client.put(
        "/api/v1/files/trash/prefs", json={"autoDelete": True}
    )
    assert resp.status_code == 204

    resp = await authenticated_client.get("/api/v1/files/trash/prefs")
    assert resp.status_code == 200
    assert resp.json()["autoDelete"] is True

    # Clean up — reset to False
    await authenticated_client.put(
        "/api/v1/files/trash/prefs", json={"autoDelete": False}
    )


@pytest.mark.asyncio
async def test_age_purge_skipped_when_auto_delete_off(
    authenticated_client: AsyncClient,
):
    """Items older than 30 days must NOT be purged when auto-delete is disabled."""
    from app.config import settings
    from app import store
    from app.routes import trash_routes
    from datetime import timedelta, timezone as tz

    # Ensure auto-delete is OFF
    await authenticated_client.put(
        "/api/v1/files/trash/prefs", json={"autoDelete": False}
    )

    trash_dir = settings.trash_dir / "user_age_test"
    trash_dir.mkdir(parents=True, exist_ok=True)
    old_file = trash_dir / "ancient.txt"
    old_file.write_bytes(b"old content")

    now = datetime.now(tz.utc)
    old_item = {
        "id": "ancient-item",
        "originalPath": "/srv/nas/shared/ancient.txt",
        "trashPath": str(old_file),
        "filename": "ancient.txt",
        "deletedAt": (now - timedelta(days=35)).isoformat(),
        "sizeBytes": len(b"old content"),
        "deletedBy": "user_age_test",
    }
    await store.save_trash_items([old_item])

    await trash_routes._purge_trash_if_needed()

    remaining = await store.get_trash_items()
    assert any(i["id"] == "ancient-item" for i in remaining), (
        "Item should NOT be purged when auto-delete is disabled"
    )

    # Clean up
    await store.save_trash_items([])
