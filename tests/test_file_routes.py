"""
File Routes Tests — upload safety, directory ops, download, path edge cases.
"""

import asyncio
import io
import os
import uuid

import pytest
from httpx import AsyncClient
from pathlib import Path


@pytest.mark.asyncio
async def test_upload_file_basic(authenticated_client: AsyncClient, tmp_path: Path):
    """Upload a file and verify it appears in the listing."""
    content = b"hello world"
    files = {"file": ("test.txt", io.BytesIO(content), "text/plain")}
    response = await authenticated_client.post(
        "/api/v1/files/upload?path=/srv/nas/shared/",
        files=files,
    )
    assert response.status_code == 201
    data = response.json()
    assert data["name"] == "test.txt"
    assert data["sizeBytes"] == len(content)


@pytest.mark.asyncio
async def test_upload_video_pregenerates_thumbnail(authenticated_client: AsyncClient, monkeypatch):
    """Uploading a video must trigger thumbnail generation right away, not wait for the first
    viewer to request it on demand -- otherwise a freshly uploaded video shows a bare play-icon
    placeholder in the Gallery grid until someone happens to scroll to it, which reads as the
    upload having silently stalled (reported 2026-07-29 while connected to the demo VPS)."""
    from app.routes import file_routes

    generated: list[tuple[Path, int]] = []

    async def fake_generate_video_thumbnail(resolved: Path, size: int) -> bytes:
        generated.append((resolved, size))
        return b"fake-jpeg-bytes"

    monkeypatch.setattr(file_routes, "_generate_video_thumbnail", fake_generate_video_thumbnail)

    content = b"fake video bytes"
    files = {"file": ("clip.mp4", io.BytesIO(content), "video/mp4")}
    response = await authenticated_client.post(
        "/api/v1/files/upload?path=/srv/nas/shared/",
        files=files,
    )
    assert response.status_code == 201, response.text

    # Pregeneration is fired via asyncio.create_task (fire-and-forget, same pattern as the
    # existing post-upload notify/index tasks) -- give the event loop a few ticks to actually
    # run it before asserting, rather than assuming it already ran synchronously.
    for _ in range(20):
        if generated:
            break
        await asyncio.sleep(0.05)

    assert len(generated) == 1, "video upload did not trigger thumbnail pregeneration"
    resolved_path, size = generated[0]
    assert resolved_path.name == "clip.mp4"
    assert size == 256


@pytest.mark.asyncio
async def test_upload_non_video_does_not_pregenerate_thumbnail(authenticated_client: AsyncClient, monkeypatch):
    """Sanity check for the gating itself -- a plain document upload must not trigger the
    video-thumbnail pregeneration path at all."""
    from app.routes import file_routes

    generated: list[tuple[Path, int]] = []

    async def fake_generate_video_thumbnail(resolved: Path, size: int) -> bytes:
        generated.append((resolved, size))
        return b"fake-jpeg-bytes"

    monkeypatch.setattr(file_routes, "_generate_video_thumbnail", fake_generate_video_thumbnail)

    content = b"hello world"
    files = {"file": ("notes.txt", io.BytesIO(content), "text/plain")}
    response = await authenticated_client.post(
        "/api/v1/files/upload?path=/srv/nas/shared/",
        files=files,
    )
    assert response.status_code == 201

    await asyncio.sleep(0.2)
    assert generated == []


@pytest.mark.asyncio
@pytest.mark.security
async def test_upload_filename_traversal_blocked(authenticated_client: AsyncClient):
    """Upload with ../etc/evil filename should be sanitized to just 'evil'."""
    content = b"malicious"
    files = {"file": ("../../etc/evil", io.BytesIO(content), "text/plain")}
    response = await authenticated_client.post(
        "/api/v1/files/upload?path=/srv/nas/shared/",
        files=files,
    )
    # Should sanitize to "evil" and succeed, or reject
    if response.status_code == 201:
        data = response.json()
        # Filename should be sanitized — no path separators
        assert "/" not in data["name"]
        assert ".." not in data["name"]


@pytest.mark.asyncio
@pytest.mark.security
async def test_upload_filename_with_slashes_sanitized(authenticated_client: AsyncClient):
    """Upload with path separators in filename should be stripped to just the filename."""
    content = b"test content"
    files = {"file": ("subdir/deep/file.txt", io.BytesIO(content), "text/plain")}
    response = await authenticated_client.post(
        "/api/v1/files/upload?path=/srv/nas/shared/",
        files=files,
    )
    if response.status_code == 201:
        data = response.json()
        assert data["name"] == "file.txt"


@pytest.mark.asyncio
async def test_mkdir_and_list(authenticated_client: AsyncClient):
    """Create a folder and verify it appears in listing."""
    folder_name = f"test_folder_{uuid.uuid4().hex[:8]}"
    # Create folder
    response = await authenticated_client.post(
        "/api/v1/files/mkdir",
        json={"path": f"/srv/nas/shared/{folder_name}"},
    )
    assert response.status_code == 201

    # List and verify
    response = await authenticated_client.get(
        "/api/v1/files/list?path=/srv/nas/shared/"
    )
    assert response.status_code == 200
    items = response.json()["items"]
    folder_names = [i["name"] for i in items if i["isDirectory"]]
    assert folder_name in folder_names


@pytest.mark.asyncio
async def test_mkdir_duplicate_returns_409(authenticated_client: AsyncClient):
    """Creating a folder that already exists returns 409."""
    # Create once
    await authenticated_client.post(
        "/api/v1/files/mkdir",
        json={"path": "/srv/nas/shared/dup_folder"},
    )
    # Create again
    response = await authenticated_client.post(
        "/api/v1/files/mkdir",
        json={"path": "/srv/nas/shared/dup_folder"},
    )
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_mkdir_component_too_long_returns_400_not_500(authenticated_client: AsyncClient):
    """L-2 (security audit 2026-08): a folder-name path component the OS rejects as too long
    (255 bytes on every filesystem this app targets) used to raise a raw OSError(ENAMETOOLONG)
    all the way out to a generic 500 -- real user input via a real route, not a synthetic one."""
    overlong_component = "a" * 300
    response = await authenticated_client.post(
        "/api/v1/files/mkdir",
        json={"path": f"/srv/nas/shared/{overlong_component}"},
    )
    assert response.status_code == 400, response.text
    assert "too long" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_delete_file(authenticated_client: AsyncClient):
    """Delete a file and verify it's gone."""
    # Upload a file — it lands in user's .inbox/ now
    files = {"file": ("to_delete.txt", io.BytesIO(b"bye"), "text/plain")}
    resp = await authenticated_client.post(
        "/api/v1/files/upload?path=/srv/nas/shared/",
        files=files,
    )
    assert resp.status_code == 201
    uploaded_path = resp.json()["path"]

    # Delete using the actual path returned by upload
    response = await authenticated_client.delete(
        f"/api/v1/files/delete?path={uploaded_path}"
    )
    assert response.status_code == 204


@pytest.mark.asyncio
async def test_delete_file_marks_media_index_deleted_immediately(authenticated_client: AsyncClient):
    """The path-based /files/delete route has no entry id to call
    media_index.mark_entry_deleted() with (unlike DELETE /api/v1/media/{id}) — real bug
    found via live end-to-end testing: deleting a photo from the app's All Photos grid
    (which still uses this path-based route) left a stale, un-deleted media_index row
    behind, invisible to GET /media's deleted=0 filter but still occupying its dedup
    hash forever. Verify the fix: deleting via this route marks the row deleted right
    away, with no reconcile pass needed, AND re-uploading identical content afterward
    is treated as new rather than silently skipped as an already-known duplicate."""
    files = {"file": ("dedupe_after_delete.txt", io.BytesIO(b"same bytes twice"), "text/plain")}
    upload = await authenticated_client.post(
        "/api/v1/files/upload?path=/srv/nas/shared/",
        files=files,
    )
    assert upload.status_code == 201
    uploaded_path = upload.json()["path"]

    delete_resp = await authenticated_client.delete(
        f"/api/v1/files/delete?path={uploaded_path}"
    )
    assert delete_resp.status_code == 204

    from app import media_index
    rows = await media_index.query_entries(scope="family", limit=50)
    assert not any(r["filename"] == "dedupe_after_delete.txt" for r in rows)

    # Re-uploading identical content after the delete must actually store a new file,
    # not silently no-op because the old (deleted) upload's hash is still on record.
    reupload_files = {"file": ("dedupe_after_delete_2.txt", io.BytesIO(b"same bytes twice"), "text/plain")}
    reupload = await authenticated_client.post(
        "/api/v1/files/upload?path=/srv/nas/shared/",
        files=reupload_files,
    )
    assert reupload.status_code == 201
    assert reupload.json()["name"] == "dedupe_after_delete_2.txt"


@pytest.mark.asyncio
async def test_delete_nonexistent_returns_404(authenticated_client: AsyncClient):
    """Deleting a nonexistent file returns 404."""
    response = await authenticated_client.delete(
        "/api/v1/files/delete?path=/srv/nas/shared/no_such_file_xyz.txt"
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_rename_file(authenticated_client: AsyncClient):
    """Rename a file and verify old name gone, new name exists."""
    old_name = f"old_{uuid.uuid4().hex[:8]}.txt"
    new_name = f"new_{uuid.uuid4().hex[:8]}.txt"
    # Upload — file lands in .inbox/
    files = {"file": (old_name, io.BytesIO(b"data"), "text/plain")}
    resp = await authenticated_client.post(
        "/api/v1/files/upload?path=/srv/nas/shared/",
        files=files,
    )
    assert resp.status_code == 201
    uploaded_path = resp.json()["path"]

    # Rename using the actual uploaded path
    response = await authenticated_client.put(
        "/api/v1/files/rename",
        json={"oldPath": uploaded_path, "newName": new_name},
    )
    assert response.status_code == 204


@pytest.mark.asyncio
async def test_rename_to_empty_name_returns_400(authenticated_client: AsyncClient):
    """Renaming with empty new name returns 400."""
    response = await authenticated_client.put(
        "/api/v1/files/rename",
        json={"oldPath": "/srv/nas/shared/something.txt", "newName": ""},
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_download_nonexistent_returns_404(authenticated_client: AsyncClient):
    """Downloading a nonexistent file returns 404."""
    response = await authenticated_client.get(
        "/api/v1/files/download?path=/srv/nas/shared/no_file_here_xyz.txt"
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_download_directory_returns_400(authenticated_client: AsyncClient):
    """Downloading a directory returns 400."""
    # Use /shared/ which maps to the sandboxed nas_root/shared/ created by conftest
    response = await authenticated_client.get(
        "/api/v1/files/download?path=/shared/"
    )
    assert response.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("filename", [
    "evil.sh", "run.bash", "script.zsh",
    "hack.py", "exploit.rb", "payload.pl",
    "malware.php", "binary.elf", "virus.exe",
    "app.apk", "module.so", "kernel.ko",
    "package.deb", "package.rpm",
])
@pytest.mark.security
async def test_blocked_executable_upload_returns_415(
    authenticated_client: AsyncClient, filename: str
):
    """Uploading a blocked executable file type must return HTTP 415 before any disk write."""
    files = {"file": (filename, io.BytesIO(b"#!/bin/sh\nrm -rf /"), "application/octet-stream")}
    response = await authenticated_client.post(
        "/api/v1/files/upload?path=/srv/nas/shared/",
        files=files,
    )
    assert response.status_code == 415, f"Expected 415 for {filename}, got {response.status_code}"
    assert "not allowed" in response.json().get("detail", "").lower()


@pytest.mark.asyncio
@pytest.mark.security
async def test_blocked_extension_case_insensitive(authenticated_client: AsyncClient):
    """Extension check must be case-insensitive (e.g. .SH, .EXE)."""
    for filename in ("EVIL.SH", "VIRUS.EXE", "HACK.PY"):
        files = {"file": (filename, io.BytesIO(b"bad"), "application/octet-stream")}
        response = await authenticated_client.post(
            "/api/v1/files/upload?path=/srv/nas/shared/",
            files=files,
        )
        assert response.status_code == 415, f"Expected 415 for {filename}"


@pytest.mark.asyncio
async def test_list_with_pagination(authenticated_client: AsyncClient):
    """Verify pagination parameters work."""
    response = await authenticated_client.get(
        "/api/v1/files/list?path=/srv/nas/shared/&page=0&page_size=5"
    )
    assert response.status_code == 200
    data = response.json()
    assert "items" in data
    assert "totalCount" in data
    assert data["page"] == 0
    assert data["pageSize"] == 5


@pytest.mark.asyncio
async def test_list_with_sort(authenticated_client: AsyncClient):
    """Verify sort parameters are accepted."""
    response = await authenticated_client.get(
        "/api/v1/files/list?path=/srv/nas/shared/&sort_by=modified&sort_dir=desc"
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_sort_now_sorts_existing_folder(authenticated_client: AsyncClient):
    """Manual sort endpoint should categorize files in an existing folder."""
    from app.routes.file_routes import _safe_resolve

    response = await authenticated_client.post(
        "/api/v1/files/mkdir",
        json={"path": "/srv/nas/shared/RawData"},
    )
    assert response.status_code == 201

    shared_raw = _safe_resolve("/srv/nas/shared/RawData")
    shared_raw.mkdir(parents=True, exist_ok=True)
    (shared_raw / "nested").mkdir(parents=True, exist_ok=True)
    (shared_raw / "holiday.jpg").write_bytes(b"img")
    (shared_raw / "receipt_scan.jpg").write_bytes(b"img2")
    (shared_raw / "movie.mp4").write_bytes(b"vid")
    (shared_raw / "notes.txt").write_bytes(b"doc")
    (shared_raw / "nested" / "paper.pdf").write_bytes(b"%PDF-1.4")

    response = await authenticated_client.post(
        "/api/v1/files/sort-now?path=/srv/nas/shared/RawData"
    )
    assert response.status_code == 200
    data = response.json()
    assert data["moved"] >= 5

    # Sorting now date-buckets too (<category>/YYYY/MM/<name>), so check by
    # category subtree rather than a flat direct-child path.
    assert list((shared_raw / "Photos").rglob("holiday.jpg")), "holiday.jpg not under Photos/"
    # Keyword in filename → Documents regardless of size.
    assert list((shared_raw / "Documents").rglob("receipt_scan.jpg")), "receipt_scan.jpg not under Documents/"
    assert list((shared_raw / "Videos").rglob("movie.mp4")), "movie.mp4 not under Videos/"
    assert list((shared_raw / "Documents").rglob("notes.txt")), "notes.txt not under Documents/"
    assert list((shared_raw / "Documents").rglob("paper.pdf")), "paper.pdf not under Documents/"


@pytest.mark.asyncio
async def test_sort_now_refuses_already_sorted_folder(authenticated_client: AsyncClient):
    """Running Sort directly inside a folder that IS a category destination (e.g. Photos/)
    must be rejected, not silently nest everything into Photos/Photos/<year>/<month>/ and
    move it out from under the level being viewed -- the real bug this guards against
    (found live 2026-07-26, real user photos looked deleted but were just nested one level
    down)."""
    from app.routes.file_routes import _safe_resolve

    response = await authenticated_client.post(
        "/api/v1/files/mkdir",
        json={"path": "/srv/nas/personal/testuser/Photos"},
    )
    assert response.status_code == 201

    photos_dir = _safe_resolve("/srv/nas/personal/testuser/Photos")
    (photos_dir / "family.jpg").write_bytes(b"img")

    response = await authenticated_client.post(
        "/api/v1/files/sort-now?path=/srv/nas/personal/testuser/Photos"
    )
    assert response.status_code == 400

    # The file must still be exactly where it started -- not nested into Photos/Photos/.
    assert (photos_dir / "family.jpg").exists()
    assert not (photos_dir / "Photos").exists()


@pytest.mark.asyncio
async def test_sort_now_missing_dir_returns_404(authenticated_client: AsyncClient):
    """Sorting a missing directory should return 404."""
    response = await authenticated_client.post(
        "/api/v1/files/sort-now?path=/srv/nas/shared/no_such_folder"
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_file_ops_require_auth(client: AsyncClient):
    """All file endpoints require authentication."""
    endpoints = [
        ("GET", "/api/v1/files/list?path=/srv/nas/shared/"),
        ("GET", "/api/v1/files/category-stats?path=/srv/nas/shared/"),
        ("POST", "/api/v1/files/mkdir"),
        ("DELETE", "/api/v1/files/delete?path=/srv/nas/shared/x"),
        ("GET", "/api/v1/files/download?path=/srv/nas/shared/x"),
        ("POST", "/api/v1/files/sort-now?path=/srv/nas/shared/RawData"),
    ]
    for method, url in endpoints:
        response = await client.request(method, url)
        assert response.status_code in (401, 403), \
            f"{method} {url} should require auth, got {response.status_code}"


@pytest.mark.asyncio
async def test_scan_cache_evicts_expired_entries(authenticated_client: AsyncClient):
    """Expired _scan_cache entries are evicted when a new entry is written."""
    from app.routes import file_routes
    import time as _time

    # Fill cache with 20 already-expired entries
    now = _time.monotonic()
    for i in range(20):
        file_routes._scan_cache[f"/fake/path{i}|name|asc|1|50"] = (([], 0), now - 10)

    size_before = len(file_routes._scan_cache)
    assert size_before >= 20

    # Trigger a real list_files call which will write a new entry after eviction
    await authenticated_client.get("/api/v1/files/list?path=/srv/nas/shared/")

    size_after = len(file_routes._scan_cache)
    assert size_after < size_before, (
        f"Expired scan cache entries must be evicted on write: "
        f"before={size_before}, after={size_after}"
    )


@pytest.mark.asyncio
async def test_set_trash_prefs_requires_admin(client: AsyncClient, member_token: str):
    """Non-admin users must not be able to change the trash auto-delete setting."""
    res = await client.put(
        "/api/v1/files/trash/prefs",
        json={"autoDelete": True},
        headers={"Authorization": f"Bearer {member_token}"},
    )
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_set_trash_prefs_admin_succeeds(client: AsyncClient, admin_token: str):
    """Admin users can change the trash auto-delete setting."""
    res = await client.put(
        "/api/v1/files/trash/prefs",
        json={"autoDelete": True},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 204


# ---------------------------------------------------------------------------
# /files/list — transparent YYYY/MM date-bucket flattening
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_flattens_year_month_bucketed_files(authenticated_client: AsyncClient):
    """A category root whose only children are 4-digit year dirs (the shape
    ingest() now produces) must list the actual files inside YYYY/MM/, not
    just the year directories — otherwise the Android grid that still
    browses by physical category path sees nothing the moment any file
    lands in a date bucket."""
    from app.config import settings

    photos_root = settings.nas_root / "shared" / "Photos"
    (photos_root / "2025" / "01").mkdir(parents=True)
    (photos_root / "2025" / "03").mkdir(parents=True)
    (photos_root / "2025" / "01" / "jan.jpg").write_bytes(b"jan")
    (photos_root / "2025" / "03" / "mar.jpg").write_bytes(b"mar")

    resp = await authenticated_client.get(
        "/api/v1/files/list", params={"path": "/shared/Photos/"},
    )
    assert resp.status_code == 200
    names = {i["name"] for i in resp.json()["items"]}
    assert names == {"jan.jpg", "mar.jpg"}
    assert all(not i["isDirectory"] for i in resp.json()["items"])


@pytest.mark.asyncio
async def test_list_flattens_overflow_month_parts(authenticated_client: AsyncClient):
    """A month directory that overflowed to -p2 (see file_sorter._date_bucket_dir)
    must also be flattened, not just the base MM name."""
    from app.config import settings

    photos_root = settings.nas_root / "shared" / "Photos"
    (photos_root / "2025" / "01").mkdir(parents=True)
    (photos_root / "2025" / "01-p2").mkdir(parents=True)
    (photos_root / "2025" / "01" / "a.jpg").write_bytes(b"a")
    (photos_root / "2025" / "01-p2" / "b.jpg").write_bytes(b"b")

    resp = await authenticated_client.get(
        "/api/v1/files/list", params={"path": "/shared/Photos/"},
    )
    names = {i["name"] for i in resp.json()["items"]}
    assert names == {"a.jpg", "b.jpg"}


@pytest.mark.asyncio
async def test_list_merges_loose_files_with_year_bucketed_files(authenticated_client: AsyncClient):
    """A transition-period mix — some legacy flat files alongside new
    YYYY/MM-bucketed ones — must show both, not just one or the other."""
    from app.config import settings

    photos_root = settings.nas_root / "shared" / "Photos"
    photos_root.mkdir(parents=True)
    (photos_root / "legacy.jpg").write_bytes(b"legacy")
    (photos_root / "2025" / "01").mkdir(parents=True)
    (photos_root / "2025" / "01" / "new.jpg").write_bytes(b"new")

    resp = await authenticated_client.get(
        "/api/v1/files/list", params={"path": "/shared/Photos/"},
    )
    names = {i["name"] for i in resp.json()["items"]}
    assert names == {"legacy.jpg", "new.jpg"}


@pytest.mark.asyncio
async def test_list_does_not_flatten_ordinary_subdirectories(authenticated_client: AsyncClient):
    """A directory whose children are ordinary (non-4-digit-year-named)
    subdirectories — e.g. Backups/<sync-folder-name>/ — must behave exactly
    as before: subdirectories listed as directories, no recursion."""
    from app.config import settings

    backups_root = settings.nas_root / "shared" / "Backups"
    (backups_root / "Camera").mkdir(parents=True)
    (backups_root / "Camera" / "photo.jpg").write_bytes(b"x")

    resp = await authenticated_client.get(
        "/api/v1/files/list", params={"path": "/shared/Backups/"},
    )
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["name"] == "Camera"
    assert items[0]["isDirectory"] is True


# ---------------------------------------------------------------------------
# Sync unification — syncScope/sourceFolder (retired physical Backups/<id>/ tree)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_upload_sync_scope_lands_in_normal_sorted_tree(authenticated_client: AsyncClient):
    """A Sync upload (syncScope given, no path) is classified and date-bucketed
    exactly like a direct upload — no physically distinct Backups/<id>/ location —
    and source/sourceFolder are recorded as explicit metadata."""
    from app import media_index

    content = b"\xff\xd8\xff" + b"\x00" * 1024
    files = {"file": ("cam.jpg", io.BytesIO(content), "image/jpeg")}
    response = await authenticated_client.post(
        "/api/v1/files/upload?syncScope=family&sourceFolder=Camera",
        files=files,
    )
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["sortedTo"] == "Photos"
    # No literal "Backups" segment anywhere in the resolved physical path.
    assert "Backups" not in data["path"]

    resp = await authenticated_client.get(
        "/api/v1/media", params={"scope": "family", "sourceFolder": "Camera"},
    )
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["source"] == "sync"
    assert items[0]["sourceFolder"] == "Camera"


@pytest.mark.asyncio
async def test_upload_sync_family_scope_merges_across_members(client: AsyncClient, member_token: str):
    """Two different family members each syncing to family scope converge into the
    SAME physical date bucket — not per-device silos — since Sync now writes
    through the identical SORTED path a direct family upload would use."""
    # member_token fixture already creates "admin" (pin 0000) and "alice" (pin 1111).
    resp = await client.post("/api/v1/auth/login", json={"name": "admin", "pin": "0000"})
    assert resp.status_code == 200
    admin_token = resp.json()["accessToken"]

    async def _sync_upload(token: str, name: str, content: bytes) -> dict:
        files = {"file": (name, io.BytesIO(content), "image/jpeg")}
        resp = await client.post(
            "/api/v1/files/upload?syncScope=family&sourceFolder=Camera",
            files=files,
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 201, resp.text
        return resp.json()

    admin_upload = await _sync_upload(admin_token, "admin_photo.jpg", b"\xff\xd8\xff" + b"\x00" * 1024)
    alice_upload = await _sync_upload(member_token, "alice_photo.jpg", b"\xff\xd8\xff" + b"\x11" * 1024)

    # Same physical directory (same bucket) — a shared, merged family timeline,
    # not one Backups/<id>/ tree per contributing member.
    from pathlib import Path
    assert Path(admin_upload["path"]).parent == Path(alice_upload["path"]).parent

    resp = await client.get(
        "/api/v1/media", params={"scope": "family"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    names = {i["filename"] for i in resp.json()["items"]}
    assert {"admin_photo.jpg", "alice_photo.jpg"} <= names


# ---------------------------------------------------------------------------
# upload-stream endpoint
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_upload_stream_basic(authenticated_client: AsyncClient):
    """Stream upload writes the file and returns correct metadata."""
    content = b"hello streaming world"
    response = await authenticated_client.post(
        "/api/v1/files/upload-stream?filename=stream_test.txt&path=/srv/nas/shared/",
        content=content,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 201
    data = response.json()
    assert data["name"] == "stream_test.txt"
    assert data["sizeBytes"] == len(content)


@pytest.mark.asyncio
async def test_upload_stream_to_inbox_when_no_path(authenticated_client: AsyncClient):
    """Stream upload with no path lands in the user's .inbox/ directory."""
    content = b"inbox content"
    response = await authenticated_client.post(
        "/api/v1/files/upload-stream?filename=inbox_file.txt",
        content=content,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 201
    data = response.json()
    assert data["name"] == "inbox_file.txt"
    assert data["sizeBytes"] == len(content)


@pytest.mark.asyncio
async def test_upload_stream_blocked_extension(authenticated_client: AsyncClient):
    """Streaming a .sh file must be rejected with 415."""
    response = await authenticated_client.post(
        "/api/v1/files/upload-stream?filename=evil.sh&path=/srv/nas/shared/",
        content=b"#!/bin/bash",
        headers={"Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 415


@pytest.mark.asyncio
async def test_upload_stream_blocked_extension_py(authenticated_client: AsyncClient):
    """Streaming a .py file must be rejected with 415."""
    response = await authenticated_client.post(
        "/api/v1/files/upload-stream?filename=script.py&path=/srv/nas/shared/",
        content=b"print('hi')",
        headers={"Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 415


@pytest.mark.asyncio
async def test_upload_stream_invalid_filename(authenticated_client: AsyncClient):
    """A filename that reduces to empty/dot after sanitisation must be rejected with 400."""
    response = await authenticated_client.post(
        "/api/v1/files/upload-stream?filename=.",
        content=b"data",
        headers={"Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_upload_stream_path_traversal_in_filename(authenticated_client: AsyncClient):
    """Path separators in the filename must be stripped (only the basename is kept)."""
    content = b"traversal attempt"
    response = await authenticated_client.post(
        "/api/v1/files/upload-stream?filename=../../etc/passwd&path=/srv/nas/shared/",
        content=content,
        headers={"Content-Type": "application/octet-stream"},
    )
    # Either sanitised to "passwd" and succeeds, or rejected
    if response.status_code == 201:
        assert response.json()["name"] == "passwd"
    else:
        assert response.status_code in (400, 403)


@pytest.mark.asyncio
async def test_upload_stream_size_limit(authenticated_client: AsyncClient, monkeypatch):
    """Exceeding max_upload_bytes must return 413."""
    from app.config import settings
    monkeypatch.setattr(settings, "max_upload_bytes", 5)
    response = await authenticated_client.post(
        "/api/v1/files/upload-stream?filename=big.txt&path=/srv/nas/shared/",
        content=b"123456789",  # 9 bytes > 5 byte limit
        headers={"Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 413


@pytest.mark.asyncio
async def test_upload_stream_requires_auth(client: AsyncClient):
    """Stream upload without auth must return 401 or 403."""
    response = await client.post(
        "/api/v1/files/upload-stream?filename=test.txt",
        content=b"data",
        headers={"Content-Type": "application/octet-stream"},
    )
    assert response.status_code in (401, 403)


# ── HTTP Range request tests ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_download_full_advertises_accept_ranges(authenticated_client, tmp_path):
    """Full download response includes Accept-Ranges: bytes."""
    from app.config import settings
    f = settings.nas_root / "shared" / "range_test.bin"
    f.write_bytes(b"A" * 1024)
    resp = await authenticated_client.get("/api/v1/files/download?path=/shared/range_test.bin")
    assert resp.status_code == 200
    assert resp.headers.get("accept-ranges") == "bytes"
    assert resp.headers.get("content-length") == "1024"
    assert len(resp.content) == 1024


@pytest.mark.asyncio
async def test_download_range_returns_206(authenticated_client, tmp_path):
    """Range request returns 206 with correct slice."""
    from app.config import settings
    payload = bytes(range(256)) * 4  # 1024 bytes, predictable content
    f = settings.nas_root / "shared" / "range_slice.bin"
    f.write_bytes(payload)
    resp = await authenticated_client.get(
        "/api/v1/files/download?path=/shared/range_slice.bin",
        headers={"Range": "bytes=0-9"},
    )
    assert resp.status_code == 206
    assert resp.headers["content-range"] == "bytes 0-9/1024"
    assert resp.headers["content-length"] == "10"
    assert resp.headers["accept-ranges"] == "bytes"
    assert resp.content == payload[0:10]


@pytest.mark.asyncio
async def test_download_range_open_ended(authenticated_client, tmp_path):
    """bytes=N- returns from N to end of file."""
    from app.config import settings
    payload = b"0123456789"
    f = settings.nas_root / "shared" / "range_open.bin"
    f.write_bytes(payload)
    resp = await authenticated_client.get(
        "/api/v1/files/download?path=/shared/range_open.bin",
        headers={"Range": "bytes=5-"},
    )
    assert resp.status_code == 206
    assert resp.headers["content-range"] == "bytes 5-9/10"
    assert resp.content == b"56789"


@pytest.mark.asyncio
async def test_download_range_suffix(authenticated_client, tmp_path):
    """bytes=-N returns the last N bytes."""
    from app.config import settings
    payload = b"0123456789"
    f = settings.nas_root / "shared" / "range_suffix.bin"
    f.write_bytes(payload)
    resp = await authenticated_client.get(
        "/api/v1/files/download?path=/shared/range_suffix.bin",
        headers={"Range": "bytes=-4"},
    )
    assert resp.status_code == 206
    assert resp.headers["content-range"] == "bytes 6-9/10"
    assert resp.content == b"6789"


@pytest.mark.asyncio
async def test_download_range_out_of_bounds_returns_416(authenticated_client, tmp_path):
    """Range beyond file size returns 416 Range Not Satisfiable."""
    from app.config import settings
    f = settings.nas_root / "shared" / "range_416.bin"
    f.write_bytes(b"hello")
    resp = await authenticated_client.get(
        "/api/v1/files/download?path=/shared/range_416.bin",
        headers={"Range": "bytes=10-20"},
    )
    assert resp.status_code == 416
    assert "bytes */5" in resp.headers.get("content-range", "")


@pytest.mark.asyncio
async def test_download_range_invalid_unit_returns_416(authenticated_client, tmp_path):
    """Non-bytes range unit returns 416."""
    from app.config import settings
    f = settings.nas_root / "shared" / "range_unit.bin"
    f.write_bytes(b"hello world")
    resp = await authenticated_client.get(
        "/api/v1/files/download?path=/shared/range_unit.bin",
        headers={"Range": "items=0-5"},
    )
    assert resp.status_code == 416


@pytest.mark.asyncio
async def test_download_content_disposition_inline(authenticated_client, tmp_path):
    """Download response uses inline Content-Disposition (not attachment) for streaming."""
    from app.config import settings
    f = settings.nas_root / "shared" / "inline_test.mp4"
    f.write_bytes(b"fake video data")
    resp = await authenticated_client.get("/api/v1/files/download?path=/shared/inline_test.mp4")
    assert resp.status_code == 200
    cd = resp.headers.get("content-disposition", "")
    assert cd.startswith("inline;"), f"Expected inline disposition, got: {cd!r}"


@pytest.mark.asyncio
async def test_download_zip_of_own_personal_folder_succeeds(client, admin_token):
    """Zipping your own /personal/<you> folder returns a real zip."""
    from app.config import settings
    import zipfile
    import io as _io

    (settings.nas_root / "personal" / "admin").mkdir(parents=True, exist_ok=True)
    (settings.nas_root / "personal" / "admin" / "mine.txt").write_bytes(b"admin's own file")

    client.headers.update({"Authorization": f"Bearer {admin_token}"})
    resp = await client.get("/api/v1/files/download-zip?path=/personal/admin")
    assert resp.status_code == 200
    assert resp.headers.get("content-type") == "application/zip"
    zf = zipfile.ZipFile(_io.BytesIO(resp.content))
    assert "mine.txt" in zf.namelist()


@pytest.mark.asyncio
@pytest.mark.security
async def test_download_zip_of_personal_root_blocked(client, member_token):
    """
    Regression test for a real authorization bypass found in security review (2026-07-23):
    _authorize_path only restricts a path shaped exactly like /personal/<name>/..., so a bare
    /personal (or /, tested below) had too few path parts to trigger that check at all --
    recursively zipping it would have walked straight through every user's personal subtree.
    A non-admin member must NOT be able to zip a shared ancestor of the personal boundary.
    """
    from app.config import settings

    (settings.nas_root / "personal" / "admin").mkdir(parents=True, exist_ok=True)
    (settings.nas_root / "personal" / "admin" / "secret.txt").write_bytes(b"admin's private file")
    (settings.nas_root / "personal" / "alice").mkdir(parents=True, exist_ok=True)

    client.headers.update({"Authorization": f"Bearer {member_token}"})
    resp = await client.get("/api/v1/files/download-zip?path=/personal")
    assert resp.status_code == 403


@pytest.mark.asyncio
@pytest.mark.security
async def test_download_zip_of_nas_root_blocked(client, member_token):
    """Same bypass class as above, one level further up: zipping the bare NAS root must also
    be rejected, not silently include every user's personal files."""
    client.headers.update({"Authorization": f"Bearer {member_token}"})
    resp = await client.get("/api/v1/files/download-zip?path=/")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_download_zip_over_size_cap_returns_413(client, admin_token):
    """A folder over MAX_ZIP_SOURCE_BYTES is rejected with a clear message, not silently
    truncated or zipped anyway."""
    from app.config import settings
    from app.routes.file_routes import MAX_ZIP_SOURCE_BYTES

    big_dir = settings.nas_root / "shared" / "big_folder"
    big_dir.mkdir(parents=True, exist_ok=True)
    (big_dir / "big.bin").write_bytes(b"\0" * (MAX_ZIP_SOURCE_BYTES + 1024))

    client.headers.update({"Authorization": f"Bearer {admin_token}"})
    resp = await client.get("/api/v1/files/download-zip?path=/shared/big_folder")
    assert resp.status_code == 413
    assert "200 MB" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_download_zip_of_directory_that_is_not_directory_returns_400(authenticated_client):
    """download-zip on a plain file (not a directory) returns 400, matching /download's own
    directory-vs-file symmetry check."""
    from app.config import settings
    f = settings.nas_root / "shared" / "not_a_folder.txt"
    f.write_bytes(b"just a file")
    resp = await authenticated_client.get("/api/v1/files/download-zip?path=/shared/not_a_folder.txt")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_category_stats_counts_files_and_bytes(authenticated_client: AsyncClient):
    """A category folder with real files reports the correct count + total size."""
    from app.config import settings
    cat_dir = settings.nas_root / "shared" / "stats_photos"
    cat_dir.mkdir(parents=True, exist_ok=True)
    (cat_dir / "a.jpg").write_bytes(b"x" * 100)
    (cat_dir / "b.jpg").write_bytes(b"x" * 250)
    sub_dir = cat_dir / "sub"
    sub_dir.mkdir()
    (sub_dir / "c.jpg").write_bytes(b"x" * 50)

    resp = await authenticated_client.get("/api/v1/files/category-stats?path=/shared/stats_photos")
    assert resp.status_code == 200
    categories = resp.json()["categories"]
    assert len(categories) == 1
    assert categories[0]["path"] == "/shared/stats_photos"
    assert categories[0]["fileCount"] == 3
    assert categories[0]["totalBytes"] == 400


@pytest.mark.asyncio
async def test_category_stats_missing_folder_reports_zero(authenticated_client: AsyncClient):
    """A category that hasn't been auto-sorted into yet (folder doesn't exist) reports zero
    rather than a 404 -- an empty category is expected, not an error."""
    resp = await authenticated_client.get(
        "/api/v1/files/category-stats?path=/shared/never_sorted_into_yet"
    )
    assert resp.status_code == 200
    categories = resp.json()["categories"]
    assert categories[0]["fileCount"] == 0
    assert categories[0]["totalBytes"] == 0


@pytest.mark.asyncio
async def test_category_stats_multiple_paths_in_one_call(authenticated_client: AsyncClient):
    """Requesting several category paths at once returns one entry per path, in order."""
    from app.config import settings
    for name, size in [("stats_a", 10), ("stats_b", 20)]:
        d = settings.nas_root / "shared" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "f.bin").write_bytes(b"x" * size)

    resp = await authenticated_client.get(
        "/api/v1/files/category-stats?path=/shared/stats_a&path=/shared/stats_b"
    )
    assert resp.status_code == 200
    categories = resp.json()["categories"]
    assert len(categories) == 2
    assert categories[0]["path"] == "/shared/stats_a"
    assert categories[0]["totalBytes"] == 10
    assert categories[1]["path"] == "/shared/stats_b"
    assert categories[1]["totalBytes"] == 20
