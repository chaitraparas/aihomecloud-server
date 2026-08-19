"""
Stage 3 of the 2026-07-30 bug-hunt: a dynamic authorization sweep against the live app.

Static analysis and model review (Stages 1-2) can't tell whether an endpoint's authorization
actually behaves correctly at runtime -- they can only reason about what the code *should* do.
This replays real requests against the FastAPI app (via the existing AsyncClient/ASGITransport
harness) under three attacker postures and asserts the response is what the endpoint's own code
claims it should be:

1. Unauthenticated -- every endpoint that declares Depends(get_current_user) or
   Depends(require_admin) must reject a request with no Authorization header at all.
2. Non-admin on an admin-only route -- every endpoint that declares Depends(require_admin) must
   reject a authenticated-but-non-admin caller.
3. Cross-user ownership -- for every endpoint that takes a resource id (job_id, entry_id,
   item_id, ...) and is only Depends(get_current_user)-gated (not admin-only), a second family
   member must not be able to read/mutate a resource they don't own.

See docs/bug_hunt_2026-07-30/STAGE3_FINDINGS.md for the write-up of what this found.
"""

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


# ── Part 1: unauthenticated access sweep ────────────────────────────────────────
#
# Every (method, path) pair below is Depends(get_current_user) or Depends(require_admin)
# per a full grep of every route file's Depends() declarations (2026-07-30). Path params are
# filled with harmless placeholder values -- the assertion only cares that the response is a
# clean 401 (unauthenticated) and never a 200/201/204 (would mean the auth dependency didn't
# actually run) or 500 (would mean it crashed before ever reaching the auth check). A 404/422
# reached AFTER a 401 would already have fired is impossible in FastAPI (dependencies resolve
# before the endpoint body), so 401 is the only acceptable "rejected" outcome here.
UNAUTH_ENDPOINTS = [
    ("GET", "/api/v1/activity/events"),
    ("POST", "/api/v1/auth/logout"),
    ("PUT", "/api/v1/users/pin"),
    ("GET", "/api/v1/users/me"),
    ("PUT", "/api/v1/users/me"),
    ("POST", "/api/v1/users/avatar"),
    ("DELETE", "/api/v1/users/avatar"),
    ("DELETE", "/api/v1/users/me"),
    ("DELETE", "/api/v1/users/pin"),
    ("POST", "/api/v1/backup/check-duplicate"),
    ("POST", "/api/v1/backup/record-hash"),
    ("GET", "/api/v1/backup/status"),
    ("POST", "/api/v1/backup/jobs"),
    ("DELETE", "/api/v1/backup/jobs/x"),
    ("POST", "/api/v1/backup/jobs/x/report"),
    ("POST", "/api/v1/backup/notify"),
    ("GET", "/api/v1/backup/duplicates"),
    ("POST", "/api/v1/backup/duplicates/scan"),
    ("DELETE", "/api/v1/backup/duplicates/x"),
    ("GET", "/api/v1/bluetooth/status"),
    ("PUT", "/api/v1/bluetooth/power"),
    ("POST", "/api/v1/bluetooth/scan"),
    ("POST", "/api/v1/bluetooth/pair"),
    ("POST", "/api/v1/bluetooth/connect"),
    ("GET", "/api/v1/users/family"),
    ("POST", "/api/v1/users/family"),
    ("DELETE", "/api/v1/users/family/x"),
    ("PUT", "/api/v1/users/family/x/role"),
    ("GET", "/api/v1/files/list"),
    ("POST", "/api/v1/files/mkdir"),
    ("DELETE", "/api/v1/files/delete"),
    ("PUT", "/api/v1/files/rename"),
    ("POST", "/api/v1/files/upload"),
    ("POST", "/api/v1/files/upload-stream"),
    ("GET", "/api/v1/files/download"),
    ("GET", "/api/v1/files/download-zip"),
    ("GET", "/api/v1/files/search"),
    ("POST", "/api/v1/files/sort-now"),
    ("GET", "/api/v1/files/roots"),
    ("GET", "/api/v1/files/thumbnail"),
    ("POST", "/api/v1/files/reindex"),
    ("POST", "/api/v1/files/reindex/cancel"),
    ("GET", "/api/v1/files/media-token"),
    ("GET", "/api/v1/files/trash"),
    ("POST", "/api/v1/files/trash/x/restore"),
    ("DELETE", "/api/v1/files/trash/x"),
    ("GET", "/api/v1/files/trash/prefs"),
    ("PUT", "/api/v1/files/trash/prefs"),
    ("GET", "/api/v1/jobs/x"),
    ("GET", "/api/v1/local-backup/status"),
    ("POST", "/api/v1/local-backup/mount"),
    ("POST", "/api/v1/local-backup/unmount"),
    ("POST", "/api/v1/local-backup/protected-folders"),
    ("DELETE", "/api/v1/local-backup/protected-folders"),
    ("POST", "/api/v1/local-backup/sync-now"),
    ("POST", "/api/v1/local-backup/media/enable"),
    ("POST", "/api/v1/local-backup/media/disable"),
    ("POST", "/api/v1/local-backup/media/sync-now"),
    ("POST", "/api/v1/local-backup/restore"),
    ("GET", "/api/v1/local-backup/browse"),
    ("GET", "/api/v1/local-backup/download"),
    ("GET", "/api/v1/media"),
    ("GET", "/api/v1/media/1/content"),
    ("GET", "/api/v1/media/1/thumbnail"),
    ("DELETE", "/api/v1/media/1"),
    ("GET", "/api/v1/network/status"),
    ("GET", "/api/v1/network/wifi"),
    ("PUT", "/api/v1/network/wifi"),
    ("GET", "/api/v1/network/wifi/scan"),
    ("POST", "/api/v1/network/wifi/connect"),
    ("POST", "/api/v1/network/wifi/forget"),
    ("GET", "/api/v1/network/hotspot"),
    ("POST", "/api/v1/network/hotspot/enable"),
    ("POST", "/api/v1/network/hotspot/disable"),
    ("GET", "/api/v1/services"),
    ("POST", "/api/v1/services/x/toggle"),
    ("GET", "/api/v1/storage/devices"),
    ("GET", "/api/v1/storage/scan"),
    ("POST", "/api/v1/storage/smart-activate"),
    ("GET", "/api/v1/storage/check-usage"),
    ("POST", "/api/v1/storage/format"),
    ("POST", "/api/v1/storage/mount"),
    ("POST", "/api/v1/storage/unmount"),
    ("POST", "/api/v1/storage/eject"),
    ("GET", "/api/v1/storage/stats"),
    ("POST", "/api/v1/storage/recover"),
    ("GET", "/api/v1/system/info"),
    ("GET", "/api/v1/system/arch"),
    ("GET", "/api/v1/system/firmware"),
    ("POST", "/api/v1/system/update"),
    ("GET", "/api/v1/system/app-update/manifest"),
    ("GET", "/api/v1/system/app-update/apk"),
    ("PUT", "/api/v1/system/name"),
    ("POST", "/api/v1/system/shutdown"),
    ("POST", "/api/v1/system/reboot"),
    ("POST", "/api/v1/system/factory-reset"),
    ("GET", "/api/v1/telegram/config"),
    ("POST", "/api/v1/telegram/config"),
    ("POST", "/api/v1/telegram/setup-local-api"),
    ("POST", "/api/v1/telegram/setup-local-api/cancel"),
    ("POST", "/api/v1/telegram/local-api/disable"),
    ("GET", "/api/v1/telegram/linked"),
    ("DELETE", "/api/v1/telegram/linked/1"),
    ("GET", "/api/v1/telegram/pending"),
    ("POST", "/api/v1/telegram/pending/1/approve"),
    ("POST", "/api/v1/telegram/pending/1/deny"),
    ("GET", "/api/v1/files/trash", ),
    ("GET", "/web/check-duplicate"),
]


@pytest.mark.parametrize("method,path", UNAUTH_ENDPOINTS)
async def test_rejects_unauthenticated(client: AsyncClient, method: str, path: str):
    resp = await client.request(method, path, params={"name": "x", "size": "1", "path": "/x"})
    assert resp.status_code == 401, (
        f"{method} {path} returned {resp.status_code} with NO Authorization header "
        f"(expected 401) -- body: {resp.text[:300]}"
    )


# ── Part 2: non-admin on admin-only routes ──────────────────────────────────────
#
# Every (method, path) pair below is Depends(require_admin) per the same grep. member_token
# belongs to "alice", a genuine non-admin family member created via the existing fixture.
ADMIN_ONLY_ENDPOINTS = [
    ("GET", "/api/v1/activity/events"),
    ("POST", "/api/v1/backup/duplicates/scan"),
    ("DELETE", "/api/v1/backup/duplicates/x"),
    ("PUT", "/api/v1/bluetooth/power"),
    ("POST", "/api/v1/bluetooth/scan"),
    ("POST", "/api/v1/bluetooth/pair"),
    ("POST", "/api/v1/bluetooth/connect"),
    ("POST", "/api/v1/users/family"),
    ("DELETE", "/api/v1/users/family/x"),
    ("PUT", "/api/v1/users/family/x/role"),
    ("POST", "/api/v1/files/reindex"),
    ("POST", "/api/v1/files/reindex/cancel"),
    ("PUT", "/api/v1/files/trash/prefs"),
    ("POST", "/api/v1/local-backup/mount"),
    ("POST", "/api/v1/local-backup/unmount"),
    ("POST", "/api/v1/local-backup/protected-folders"),
    ("DELETE", "/api/v1/local-backup/protected-folders"),
    ("POST", "/api/v1/local-backup/sync-now"),
    ("POST", "/api/v1/local-backup/media/enable"),
    ("POST", "/api/v1/local-backup/media/disable"),
    ("POST", "/api/v1/local-backup/media/sync-now"),
    ("POST", "/api/v1/local-backup/restore"),
    ("PUT", "/api/v1/network/wifi"),
    ("GET", "/api/v1/network/wifi/scan"),
    ("POST", "/api/v1/network/wifi/connect"),
    ("POST", "/api/v1/network/wifi/forget"),
    ("POST", "/api/v1/network/hotspot/enable"),
    ("POST", "/api/v1/network/hotspot/disable"),
    ("POST", "/api/v1/services/x/toggle"),
    ("POST", "/api/v1/storage/smart-activate"),
    ("POST", "/api/v1/storage/format"),
    ("POST", "/api/v1/storage/mount"),
    ("POST", "/api/v1/storage/unmount"),
    ("POST", "/api/v1/storage/eject"),
    ("POST", "/api/v1/storage/recover"),
    ("POST", "/api/v1/system/update"),
    ("PUT", "/api/v1/system/name"),
    ("POST", "/api/v1/system/shutdown"),
    ("POST", "/api/v1/system/reboot"),
    ("POST", "/api/v1/system/factory-reset"),
    ("GET", "/api/v1/telegram/config"),
    ("POST", "/api/v1/telegram/config"),
    ("POST", "/api/v1/telegram/setup-local-api"),
    ("POST", "/api/v1/telegram/setup-local-api/cancel"),
    ("POST", "/api/v1/telegram/local-api/disable"),
    ("GET", "/api/v1/telegram/linked"),
    ("DELETE", "/api/v1/telegram/linked/1"),
    ("GET", "/api/v1/telegram/pending"),
    ("POST", "/api/v1/telegram/pending/1/approve"),
    ("POST", "/api/v1/telegram/pending/1/deny"),
]


@pytest.mark.parametrize("method,path", ADMIN_ONLY_ENDPOINTS)
async def test_rejects_non_admin(client: AsyncClient, member_token: str, method: str, path: str):
    resp = await client.request(
        method, path,
        params={"name": "x", "size": "1", "path": "/x"},
        headers={"Authorization": f"Bearer {member_token}"},
    )
    assert resp.status_code == 403, (
        f"{method} {path} returned {resp.status_code} for a non-admin caller "
        f"(expected 403) -- body: {resp.text[:300]}"
    )


# ── Part 3: cross-user ownership (the real IDOR check) ──────────────────────────


async def _create_member(client: AsyncClient, admin_token: str, name: str, pin: str) -> str:
    """Create an additional non-admin family member (member_token's fixture already makes
    "alice" — this makes a second, distinct one where a test needs two non-admins)."""
    resp = await client.post(
        "/api/v1/users", json={"name": name, "pin": pin},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code in (200, 201)
    resp = await client.post("/api/v1/auth/login", json={"name": name, "pin": pin})
    assert resp.status_code == 200
    return resp.json()["accessToken"]


async def test_member_cannot_read_another_users_job_status(
    client: AsyncClient, admin_token: str, member_token: str,
):
    """jobs_routes.py's get_job_status is get_current_user-gated (not admin), with its own
    ownership check (job.user_id == caller or is_currently_admin). Fixed earlier this bug-hunt
    (jobs_routes.py trusted a stale JWT is_admin claim) -- this proves the fix holds at runtime,
    not just in the diff."""
    from app.job_store import create_job

    resp = await client.post(
        "/api/v1/auth/login", json={"name": "admin", "pin": "0000"},
    )
    admin_sub = None
    # Decode admin's own id via /users/me rather than the JWT directly (keeps this test
    # decoupled from token internals).
    me = await client.get("/api/v1/users/me", headers={"Authorization": f"Bearer {admin_token}"})
    admin_sub = me.json()["id"]

    job = create_job(user_id=admin_sub)

    resp = await client.get(
        f"/api/v1/jobs/{job.id}", headers={"Authorization": f"Bearer {member_token}"},
    )
    assert resp.status_code == 404, (
        f"non-owner member could read another user's job status: {resp.status_code} {resp.text}"
    )

    # Sanity: the owner (admin) can read it.
    resp = await client.get(
        f"/api/v1/jobs/{job.id}", headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200


async def test_member_cannot_restore_or_delete_another_users_trash_item(
    client: AsyncClient, admin_token: str, member_token: str,
):
    """trash_routes.py: fixed earlier this bug-hunt (trusted a stale JWT is_admin claim).
    Proves the fix (is_currently_admin) holds at runtime for a genuinely non-admin caller."""
    # Admin creates and deletes a personal file, producing a trash item owned by admin.
    mkdir = await client.post(
        "/api/v1/files/mkdir", json={"path": "/personal/admin"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    upload = await client.post(
        "/api/v1/files/upload",
        params={"path": "/personal/admin"},
        files={"file": ("secret.txt", b"admin's private data", "text/plain")},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert upload.status_code == 201, upload.text

    delete = await client.request(
        "DELETE", "/api/v1/files/delete",
        params={"path": "/personal/admin/secret.txt"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert delete.status_code == 204, delete.text

    trash_list = await client.get(
        "/api/v1/files/trash", headers={"Authorization": f"Bearer {admin_token}"},
    )
    items = trash_list.json()
    assert len(items) == 1
    item_id = items[0]["id"]

    restore = await client.post(
        f"/api/v1/files/trash/{item_id}/restore",
        headers={"Authorization": f"Bearer {member_token}"},
    )
    assert restore.status_code == 403, (
        f"non-owner member could restore another user's trash item: "
        f"{restore.status_code} {restore.text}"
    )

    perm_delete = await client.delete(
        f"/api/v1/files/trash/{item_id}",
        headers={"Authorization": f"Bearer {member_token}"},
    )
    assert perm_delete.status_code == 403, (
        f"non-owner member could permanently delete another user's trash item: "
        f"{perm_delete.status_code} {perm_delete.text}"
    )


async def test_member_cannot_view_or_delete_another_users_personal_media(
    client: AsyncClient, admin_token: str, member_token: str,
):
    """media_routes.py's _authorize_media_scope should block a non-owner, non-admin caller
    from another user's scope=personal media entries by opaque entry id."""
    from app import media_index

    await client.post(
        "/api/v1/files/mkdir", json={"path": "/personal/admin"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    upload = await client.post(
        "/api/v1/files/upload",
        params={"path": "/personal/admin"},
        files={"file": ("photo.jpg", b"\xff\xd8\xff\xe0fakejpegbytes", "image/jpeg")},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert upload.status_code == 201, upload.text

    await media_index.record_entry(
        rel_path="/personal/admin/photo.jpg",
        content_hash="a" * 64,
        size_bytes=20,
        scope="personal",
        owner="admin",
        category="photo",
        media_type="photo",
        source="upload",
        filename="photo.jpg",
    )
    rows = await media_index.query_entries(
        scope="personal", owner="admin", source_folder=None, category=None, media_type=None,
        sort_by="modified", sort_dir="desc", before_value=None, before_id=None, limit=10,
    )
    entry_id = rows[0]["id"]

    for method, url in (
        ("GET", f"/api/v1/media/{entry_id}/content"),
        ("GET", f"/api/v1/media/{entry_id}/thumbnail"),
        ("DELETE", f"/api/v1/media/{entry_id}"),
    ):
        resp = await client.request(
            method, url, headers={"Authorization": f"Bearer {member_token}"},
        )
        assert resp.status_code == 403, (
            f"{method} {url}: non-owner member accessed another user's personal media entry: "
            f"{resp.status_code} {resp.text}"
        )


async def test_member_cannot_see_or_mutate_another_users_backup_job(
    client: AsyncClient, admin_token: str, member_token: str,
):
    """backup_routes.py's /backup/jobs endpoints previously had no owner/userId field on the
    job dict at all, so /status leaked every family member's job list to every other member,
    and delete/report had no ownership check. Found live 2026-07-30 via Stage 3's dynamic
    authorization sweep; fixed same pass (ownerId field + _owns_job check). This test proves
    the fix holds at runtime."""
    create = await client.post(
        "/api/v1/backup/jobs",
        json={"phoneFolder": "Camera", "destination": "personal"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert create.status_code == 201, create.text
    job_id = create.json()["id"]

    status_resp = await client.get(
        "/api/v1/backup/status", headers={"Authorization": f"Bearer {member_token}"},
    )
    job_ids_visible_to_member = {j["id"] for j in status_resp.json()["jobs"]}
    assert job_id not in job_ids_visible_to_member, (
        "non-owner member could see another user's backup job in /status"
    )

    delete_resp = await client.delete(
        f"/api/v1/backup/jobs/{job_id}", headers={"Authorization": f"Bearer {member_token}"},
    )
    assert delete_resp.status_code == 403, (
        f"non-owner member could delete another user's backup job: "
        f"{delete_resp.status_code} {delete_resp.text}"
    )

    report_resp = await client.post(
        f"/api/v1/backup/jobs/{job_id}/report",
        json={"uploaded": 1, "skipped": 0, "lastSyncAt": "2026-07-30T00:00:00Z"},
        headers={"Authorization": f"Bearer {member_token}"},
    )
    assert report_resp.status_code == 403, (
        f"non-owner member could report sync stats on another user's backup job: "
        f"{report_resp.status_code} {report_resp.text}"
    )

    # Sanity: the owner (admin) still can.
    status_resp = await client.get(
        "/api/v1/backup/status", headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert job_id in {j["id"] for j in status_resp.json()["jobs"]}
    delete_resp = await client.delete(
        f"/api/v1/backup/jobs/{job_id}", headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert delete_resp.status_code == 204
