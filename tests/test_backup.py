"""
Tests for the Auto Backup endpoints (backup_routes.py).
"""
import pytest
from httpx import AsyncClient


# ── check-duplicate ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_check_duplicate_not_found(client: AsyncClient, admin_token: str):
    """A fresh hash should not be reported as a duplicate."""
    resp = await client.post(
        "/api/v1/backup/check-duplicate",
        json={"sha256": "a" * 64, "filename": "photo.jpg"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["exists"] is False


@pytest.mark.asyncio
async def test_check_duplicate_requires_auth(client: AsyncClient):
    resp = await client.post(
        "/api/v1/backup/check-duplicate",
        json={"sha256": "a" * 64, "filename": "photo.jpg"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_check_duplicate_invalid_sha(client: AsyncClient, admin_token: str):
    resp = await client.post(
        "/api/v1/backup/check-duplicate",
        json={"sha256": "tooshort", "filename": "photo.jpg"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 422


# ── record-hash ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_record_hash_and_detect_duplicate(client: AsyncClient, admin_token: str):
    """After recording a hash it should be detected as a duplicate."""
    sha = "b" * 64
    headers = {"Authorization": f"Bearer {admin_token}"}

    # Record the hash
    rec = await client.post(
        "/api/v1/backup/record-hash",
        json={"sha256": sha, "filename": "video.mp4", "destination": "personal"},
        headers=headers,
    )
    assert rec.status_code == 200
    assert rec.json()["ok"] is True

    # Now it should be found as a duplicate
    chk = await client.post(
        "/api/v1/backup/check-duplicate",
        json={"sha256": sha, "filename": "video.mp4"},
        headers=headers,
    )
    assert chk.status_code == 200
    assert chk.json()["exists"] is True


@pytest.mark.asyncio
async def test_record_hash_invalid_destination(client: AsyncClient, admin_token: str):
    resp = await client.post(
        "/api/v1/backup/record-hash",
        json={"sha256": "c" * 64, "filename": "file.jpg", "destination": "invalid"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_check_duplicate_is_not_a_cross_user_oracle(
    client: AsyncClient, admin_token: str, member_token: str,
):
    """H-4: a hash recorded by one family member must not be visible to another via
    check-duplicate — that would let anyone who knows/computes a file's hash probe whether
    someone else in the family possesses it."""
    sha = "d" * 64
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    member_headers = {"Authorization": f"Bearer {member_token}"}

    rec = await client.post(
        "/api/v1/backup/record-hash",
        json={"sha256": sha, "filename": "private.jpg", "destination": "personal"},
        headers=admin_headers,
    )
    assert rec.status_code == 200

    # The uploader sees their own hash as a duplicate.
    own_check = await client.post(
        "/api/v1/backup/check-duplicate",
        json={"sha256": sha, "filename": "private.jpg"},
        headers=admin_headers,
    )
    assert own_check.json()["exists"] is True

    # A different family member must not be able to detect it.
    other_check = await client.post(
        "/api/v1/backup/check-duplicate",
        json={"sha256": sha, "filename": "private.jpg"},
        headers=member_headers,
    )
    assert other_check.status_code == 200
    assert other_check.json()["exists"] is False


# ── status ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_backup_status_empty(client: AsyncClient, admin_token: str):
    resp = await client.get(
        "/api/v1/backup/status",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False
    assert body["jobs"] == []


@pytest.mark.asyncio
async def test_backup_status_requires_auth(client: AsyncClient):
    resp = await client.get("/api/v1/backup/status")
    assert resp.status_code == 401


# ── jobs CRUD ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_and_list_job(client: AsyncClient, admin_token: str):
    headers = {"Authorization": f"Bearer {admin_token}"}

    create = await client.post(
        "/api/v1/backup/jobs",
        json={"phoneFolder": "DCIM/Camera", "destination": "personal"},
        headers=headers,
    )
    assert create.status_code == 201
    job = create.json()
    assert job["phoneFolder"] == "DCIM/Camera"
    assert job["destination"] == "personal"
    assert job["totalUploaded"] == 0
    assert job["totalSkipped"] == 0
    assert job["lastSyncAt"] is None
    assert len(job["id"]) == 12

    status_resp = await client.get("/api/v1/backup/status", headers=headers)
    assert status_resp.status_code == 200
    body = status_resp.json()
    assert body["enabled"] is True
    assert len(body["jobs"]) == 1


@pytest.mark.asyncio
async def test_create_job_invalid_destination(client: AsyncClient, admin_token: str):
    resp = await client.post(
        "/api/v1/backup/jobs",
        json={"phoneFolder": "DCIM", "destination": "cloud"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_delete_job(client: AsyncClient, admin_token: str):
    headers = {"Authorization": f"Bearer {admin_token}"}

    create = await client.post(
        "/api/v1/backup/jobs",
        json={"phoneFolder": "WhatsApp/Media", "destination": "family"},
        headers=headers,
    )
    job_id = create.json()["id"]

    delete = await client.delete(f"/api/v1/backup/jobs/{job_id}", headers=headers)
    assert delete.status_code == 204

    status_resp = await client.get("/api/v1/backup/status", headers=headers)
    jobs = status_resp.json()["jobs"]
    assert not any(j["id"] == job_id for j in jobs)


@pytest.mark.asyncio
async def test_delete_nonexistent_job(client: AsyncClient, admin_token: str):
    resp = await client.delete(
        "/api/v1/backup/jobs/doesnotexist",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 404


# ── sync report ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_report_sync_run(client: AsyncClient, admin_token: str):
    headers = {"Authorization": f"Bearer {admin_token}"}

    create = await client.post(
        "/api/v1/backup/jobs",
        json={"phoneFolder": "DCIM/Camera", "destination": "personal"},
        headers=headers,
    )
    job_id = create.json()["id"]

    report = await client.post(
        f"/api/v1/backup/jobs/{job_id}/report",
        json={"uploaded": 12, "skipped": 3, "lastSyncAt": "2026-03-24T10:00:00+00:00"},
        headers=headers,
    )
    assert report.status_code == 200
    updated = report.json()
    assert updated["totalUploaded"] == 12
    assert updated["totalSkipped"] == 3
    assert updated["lastSyncAt"] == "2026-03-24T10:00:00+00:00"


@pytest.mark.asyncio
async def test_report_sync_accumulates(client: AsyncClient, admin_token: str):
    """Multiple reports should accumulate the uploaded/skipped counts."""
    headers = {"Authorization": f"Bearer {admin_token}"}

    create = await client.post(
        "/api/v1/backup/jobs",
        json={"phoneFolder": "DCIM", "destination": "family"},
        headers=headers,
    )
    job_id = create.json()["id"]

    await client.post(
        f"/api/v1/backup/jobs/{job_id}/report",
        json={"uploaded": 10, "skipped": 2, "lastSyncAt": "2026-03-24T10:00:00Z"},
        headers=headers,
    )
    second = await client.post(
        f"/api/v1/backup/jobs/{job_id}/report",
        json={"uploaded": 5, "skipped": 1, "lastSyncAt": "2026-03-24T16:00:00Z"},
        headers=headers,
    )
    assert second.json()["totalUploaded"] == 15
    assert second.json()["totalSkipped"] == 3


@pytest.mark.asyncio
async def test_report_nonexistent_job(client: AsyncClient, admin_token: str):
    resp = await client.post(
        "/api/v1/backup/jobs/nonexistent/report",
        json={"uploaded": 1, "skipped": 0, "lastSyncAt": "2026-03-24T10:00:00Z"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 404


# ── Concurrent job creation must not lose a job ───────────────────────────────

@pytest.mark.asyncio
async def test_concurrent_job_creation_does_not_lose_a_job(client: AsyncClient, admin_token: str):
    """
    Regression: create_backup_job used to do a get_value("backup_jobs")+set_value() pair --
    two devices setting up a backup job at the same moment could each read the same list and
    each append their own job to it, with whichever set_value ran last discarding the other's
    new job. Now wrapped in store.atomic_update, so N concurrent creates must all persist.
    """
    import asyncio

    headers = {"Authorization": f"Bearer {admin_token}"}

    async def _create(i: int):
        return await client.post(
            "/api/v1/backup/jobs",
            json={"phoneFolder": f"DCIM/Camera{i}", "destination": "personal"},
            headers=headers,
        )

    responses = await asyncio.gather(*(_create(i) for i in range(8)))
    assert all(r.status_code == 201 for r in responses)

    status_resp = await client.get("/api/v1/backup/status", headers=headers)
    assert status_resp.status_code == 200
    jobs = status_resp.json()["jobs"]
    assert len(jobs) == 8, "all 8 concurrently created jobs must be persisted, none lost"
    assert len({j["id"] for j in jobs}) == 8, "job ids must be distinct, no duplicate/collided writes"


# ── /notify — silent when nothing happened ────────────────────────────────────

@pytest.mark.asyncio
async def test_notify_silent_when_nothing_to_report(
    client: AsyncClient, admin_token: str
):
    """
    /notify must NOT send a Telegram message when success=true and both
    uploaded and skipped are zero (genuinely nothing to back up).
    The endpoint should return sent=False with reason='nothing_to_notify'.
    """
    resp = await client.post(
        "/api/v1/backup/notify",
        json={"success": True, "uploaded": 0, "skipped": 0, "folders": 2},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["sent"] is False
    assert body["reason"] == "nothing_to_notify"


@pytest.mark.asyncio
async def test_notify_sends_when_files_uploaded(
    client: AsyncClient, admin_token: str
):
    """
    /notify must attempt to send when uploaded > 0, even if Telegram is not
    configured (it will return sent=False with a different reason, not
    nothing_to_notify).
    """
    resp = await client.post(
        "/api/v1/backup/notify",
        json={"success": True, "uploaded": 5, "skipped": 2, "folders": 1},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    # In test env Telegram is not configured, so sent=False — but the reason
    # must NOT be "nothing_to_notify": the endpoint must have attempted to send.
    assert body.get("reason") != "nothing_to_notify"


@pytest.mark.asyncio
async def test_notify_requires_auth(client: AsyncClient):
    resp = await client.post(
        "/api/v1/backup/notify",
        json={"success": True, "uploaded": 0, "skipped": 0, "folders": 0},
    )
    assert resp.status_code == 401
