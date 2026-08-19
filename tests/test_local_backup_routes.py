"""
Tests for local_backup_routes.py. The `client`/`authenticated_client` fixtures
(conftest.py) already point nas_root at a tmp dir containing a `personal/`
subdirectory, which these tests reuse as a real protected-folder target.
"""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from app import local_backup


@pytest.mark.asyncio
async def test_status_requires_auth(client: AsyncClient):
    resp = await client.get("/api/v1/local-backup/status")
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_status_default_no_drive_mounted(authenticated_client: AsyncClient):
    resp = await authenticated_client.get("/api/v1/local-backup/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["driveMounted"] is False
    assert data["protectedFolders"] == []
    assert data["capacity"] is None


@pytest.mark.asyncio
async def test_mount_requires_admin(client: AsyncClient):
    resp = await client.post("/api/v1/local-backup/mount", json={"device": "/dev/sdb1"})
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_mount_nonexistent_device_404(authenticated_client: AsyncClient):
    resp = await authenticated_client.post(
        "/api/v1/local-backup/mount", json={"device": "/dev/nonexistent999"}
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_mount_refuses_primary_nas_device(authenticated_client: AsyncClient):
    with patch("app.routes.local_backup_routes.store.get_storage_state", new_callable=AsyncMock,
               return_value={"activeDevice": "/dev/sda1"}):
        resp = await authenticated_client.post(
            "/api/v1/local-backup/mount", json={"device": "/dev/sda1"}
        )
    assert resp.status_code == 400
    assert "primary NAS drive" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_mount_refuses_when_already_mounted(authenticated_client: AsyncClient):
    with patch("app.routes.local_backup_routes.local_backup.get_drive_state", new_callable=AsyncMock,
               return_value={"activeDevice": "/dev/sdb1"}):
        resp = await authenticated_client.post(
            "/api/v1/local-backup/mount", json={"device": "/dev/sdc1"}
        )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_mount_rejects_non_ext4_filesystem(authenticated_client: AsyncClient):
    # Regression pin: found live 2026-07-15 on the Cubie A5E — an exFAT drive was previously
    # accepted here, but the mount script's chown step (and local_backup's own `current`
    # symlink) can't work on exFAT, leaving the drive stuck half-mounted with no clean recovery
    # path from the app. Reject upfront instead.
    fake_partition = {"name": "sda1", "fstype": "exfat", "mountpoint": "", "model": "Ultra", "tran": "usb"}
    with patch("app.routes.local_backup_routes.find_partition", new_callable=AsyncMock,
               return_value=fake_partition), \
         patch("app.routes.local_backup_routes.is_os_partition", return_value=False):
        resp = await authenticated_client.post(
            "/api/v1/local-backup/mount", json={"device": "/dev/sda1"}
        )
    assert resp.status_code == 400
    assert "ext4" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_unmount_when_nothing_mounted_400(authenticated_client: AsyncClient):
    resp = await authenticated_client.post("/api/v1/local-backup/unmount")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_unmount_refuses_while_syncing(authenticated_client: AsyncClient):
    with patch("app.routes.local_backup_routes.local_backup.get_drive_state", new_callable=AsyncMock,
               return_value={"activeDevice": "/dev/sdb1"}), \
         patch("app.routes.local_backup_routes.local_backup.is_syncing", return_value=True):
        resp = await authenticated_client.post("/api/v1/local-backup/unmount")
    assert resp.status_code == 409


class TestProtectedFolders:
    @pytest.mark.asyncio
    async def test_add_existing_folder_succeeds(self, authenticated_client: AsyncClient):
        resp = await authenticated_client.post(
            "/api/v1/local-backup/protected-folders",
            json={"path": "/personal", "label": "Personal"},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["path"] == "/personal"
        assert body["label"] == "Personal"
        assert body["lastSyncStatus"] is None

    @pytest.mark.asyncio
    async def test_add_nonexistent_folder_404(self, authenticated_client: AsyncClient):
        resp = await authenticated_client.post(
            "/api/v1/local-backup/protected-folders",
            json={"path": "/personal/does-not-exist"},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_add_path_outside_nas_root_400(self, authenticated_client: AsyncClient):
        resp = await authenticated_client.post(
            "/api/v1/local-backup/protected-folders",
            json={"path": "/../../etc"},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_cannot_protect_the_board_identity_directory(self, authenticated_client: AsyncClient):
        """
        H-11 (docs/security/audit-2026-08/H-11_SPKI_ROTATION_DESIGN.md sec. 5): the board's
        identity key must never enter a user-facing backup — restoring one board's backup onto
        another would hand the restoring board a signature-valid clone of the original's identity,
        which is a board-impersonation kit, not a data restore. `_resolve_protected_path`'s
        `is_relative_to(nas_root)` containment check is generic and already covers this (proven
        above against /etc); this test pins the specific path H-11 cares about by name, so a
        future change narrowing that check can't silently reopen it here without failing loudly.
        In this fixture nas_root is a subdirectory of data_dir (tmp_path/"nas" under tmp_path), so
        a escape one level up from nas_root already reaches identity/ — the same shape a real
        board has (/srv/nas under /, /var/lib/aihomecloud/identity under /).
        """
        from app.config import settings
        identity_dir = settings.data_dir / "identity"
        identity_dir.mkdir()
        (identity_dir / "identity.key").write_text("fake-ed25519-key")

        resp = await authenticated_client.post(
            "/api/v1/local-backup/protected-folders",
            json={"path": "/../identity"},
        )

        assert resp.status_code == 400
        assert await local_backup.get_protected_folders() == []

    @pytest.mark.asyncio
    async def test_add_same_folder_twice_is_idempotent(self, authenticated_client: AsyncClient):
        await authenticated_client.post(
            "/api/v1/local-backup/protected-folders", json={"path": "/personal"}
        )
        await authenticated_client.post(
            "/api/v1/local-backup/protected-folders", json={"path": "/personal"}
        )
        folders = await local_backup.get_protected_folders()
        assert len(folders) == 1

    @pytest.mark.asyncio
    async def test_remove_nonexistent_folder_404(self, authenticated_client: AsyncClient):
        resp = await authenticated_client.delete(
            "/api/v1/local-backup/protected-folders", params={"path": "/personal"}
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_remove_existing_folder_succeeds(self, authenticated_client: AsyncClient):
        await authenticated_client.post(
            "/api/v1/local-backup/protected-folders", json={"path": "/personal"}
        )
        resp = await authenticated_client.delete(
            "/api/v1/local-backup/protected-folders", params={"path": "/personal"}
        )
        assert resp.status_code == 204
        assert await local_backup.get_protected_folders() == []


@pytest.mark.asyncio
async def test_sync_now_returns_started(authenticated_client: AsyncClient):
    with patch("app.routes.local_backup_routes.local_backup.sync_protected_folders", new_callable=AsyncMock):
        resp = await authenticated_client.post("/api/v1/local-backup/sync-now")
    assert resp.status_code == 202
    assert resp.json()["status"] == "started"


@pytest.mark.asyncio
async def test_sync_now_reports_already_syncing(authenticated_client: AsyncClient):
    with patch("app.routes.local_backup_routes.local_backup.is_syncing", return_value=True):
        resp = await authenticated_client.post("/api/v1/local-backup/sync-now")
    assert resp.status_code == 202
    assert resp.json()["status"] == "already_syncing"


class TestBrowseBackup:
    @pytest.mark.asyncio
    async def test_browse_requires_auth(self, client: AsyncClient):
        resp = await client.get("/api/v1/local-backup/browse")
        assert resp.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_browse_root_lists_protected_folders(self, authenticated_client: AsyncClient):
        await authenticated_client.post(
            "/api/v1/local-backup/protected-folders", json={"path": "/personal", "label": "Personal"}
        )
        resp = await authenticated_client.get("/api/v1/local-backup/browse")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["path"] == "/personal/"
        assert items[0]["isDirectory"] is True
        assert items[0]["name"] == "Personal"

    @pytest.mark.asyncio
    async def test_browse_unprotected_path_404s(self, authenticated_client: AsyncClient):
        resp = await authenticated_client.get("/api/v1/local-backup/browse", params={"path": "/family"})
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_browse_not_yet_synced_returns_empty(self, authenticated_client: AsyncClient):
        await authenticated_client.post("/api/v1/local-backup/protected-folders", json={"path": "/personal"})
        resp = await authenticated_client.get("/api/v1/local-backup/browse", params={"path": "/personal"})
        assert resp.status_code == 200
        assert resp.json()["items"] == []

    @pytest.mark.asyncio
    async def test_browse_nested_lists_real_backed_up_files(
        self, authenticated_client: AsyncClient, monkeypatch, tmp_path
    ):
        from app.config import settings
        monkeypatch.setattr(settings, "backup_root", tmp_path / "backup")

        await authenticated_client.post("/api/v1/local-backup/protected-folders", json={"path": "/personal"})
        current = tmp_path / "backup" / "personal" / "current"
        (current / "subdir").mkdir(parents=True)
        (current / "file1.txt").write_text("hello")

        resp = await authenticated_client.get("/api/v1/local-backup/browse", params={"path": "/personal"})
        assert resp.status_code == 200
        items = resp.json()["items"]
        names = {i["name"] for i in items}
        assert names == {"subdir", "file1.txt"}

        file_item = next(i for i in items if i["name"] == "file1.txt")
        assert file_item["path"] == "/personal/file1.txt"
        assert file_item["isDirectory"] is False

        dir_item = next(i for i in items if i["name"] == "subdir")
        assert dir_item["path"] == "/personal/subdir/"
        assert dir_item["isDirectory"] is True

    @pytest.mark.asyncio
    async def test_browse_two_levels_deep_keeps_every_path_segment(
        self, authenticated_client: AsyncClient, monkeypatch, tmp_path
    ):
        # Regression pin: browsing into a subfolder of a subfolder previously dropped every
        # intermediate segment (prefixed with the protected folder's own path instead of the
        # path actually being browsed) -- found live 2026-07-15 on the ROCK Pi 4A.
        from app.config import settings
        monkeypatch.setattr(settings, "backup_root", tmp_path / "backup")

        await authenticated_client.post("/api/v1/local-backup/protected-folders", json={"path": "/personal"})
        current = tmp_path / "backup" / "personal" / "current"
        (current / "subdir").mkdir(parents=True)
        (current / "subdir" / "inner.txt").write_text("nested")

        resp = await authenticated_client.get(
            "/api/v1/local-backup/browse", params={"path": "/personal/subdir"}
        )
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["path"] == "/personal/subdir/inner.txt"

    @pytest.mark.asyncio
    async def test_browse_traversal_attempt_rejected(
        self, authenticated_client: AsyncClient, monkeypatch, tmp_path
    ):
        from app.config import settings
        monkeypatch.setattr(settings, "backup_root", tmp_path / "backup")
        await authenticated_client.post("/api/v1/local-backup/protected-folders", json={"path": "/personal"})
        (tmp_path / "backup" / "personal" / "current").mkdir(parents=True)

        resp = await authenticated_client.get(
            "/api/v1/local-backup/browse", params={"path": "/personal/../../../etc"}
        )
        assert resp.status_code == 400


class TestDownloadBackup:
    @pytest.mark.asyncio
    async def test_download_requires_auth(self, client: AsyncClient):
        resp = await client.get("/api/v1/local-backup/download", params={"path": "/personal/file1.txt"})
        assert resp.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_download_unprotected_path_404s(self, authenticated_client: AsyncClient):
        resp = await authenticated_client.get(
            "/api/v1/local-backup/download", params={"path": "/family/file1.txt"}
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_download_traversal_attempt_rejected(
        self, authenticated_client: AsyncClient, monkeypatch, tmp_path
    ):
        from app.config import settings
        monkeypatch.setattr(settings, "backup_root", tmp_path / "backup")
        await authenticated_client.post("/api/v1/local-backup/protected-folders", json={"path": "/personal"})
        (tmp_path / "backup" / "personal" / "current").mkdir(parents=True)

        resp = await authenticated_client.get(
            "/api/v1/local-backup/download", params={"path": "/personal/../../../etc/passwd"}
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_download_missing_file_404s(self, authenticated_client: AsyncClient, monkeypatch, tmp_path):
        from app.config import settings
        monkeypatch.setattr(settings, "backup_root", tmp_path / "backup")
        await authenticated_client.post("/api/v1/local-backup/protected-folders", json={"path": "/personal"})
        (tmp_path / "backup" / "personal" / "current").mkdir(parents=True)

        resp = await authenticated_client.get(
            "/api/v1/local-backup/download", params={"path": "/personal/nope.txt"}
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_download_a_directory_400s(self, authenticated_client: AsyncClient, monkeypatch, tmp_path):
        from app.config import settings
        monkeypatch.setattr(settings, "backup_root", tmp_path / "backup")
        await authenticated_client.post("/api/v1/local-backup/protected-folders", json={"path": "/personal"})
        current = tmp_path / "backup" / "personal" / "current"
        (current / "subdir").mkdir(parents=True)

        resp = await authenticated_client.get(
            "/api/v1/local-backup/download", params={"path": "/personal/subdir"}
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_download_returns_real_backed_up_file_bytes(
        self, authenticated_client: AsyncClient, monkeypatch, tmp_path
    ):
        from app.config import settings
        monkeypatch.setattr(settings, "backup_root", tmp_path / "backup")
        await authenticated_client.post("/api/v1/local-backup/protected-folders", json={"path": "/personal"})
        current = tmp_path / "backup" / "personal" / "current"
        current.mkdir(parents=True)
        (current / "file1.txt").write_text("hello from backup")

        resp = await authenticated_client.get(
            "/api/v1/local-backup/download", params={"path": "/personal/file1.txt"}
        )
        assert resp.status_code == 200
        assert resp.content == b"hello from backup"
        assert "file1.txt" in resp.headers.get("content-disposition", "")

    @pytest.mark.asyncio
    async def test_download_supports_range_requests(
        self, authenticated_client: AsyncClient, monkeypatch, tmp_path
    ):
        from app.config import settings
        monkeypatch.setattr(settings, "backup_root", tmp_path / "backup")
        await authenticated_client.post("/api/v1/local-backup/protected-folders", json={"path": "/personal"})
        current = tmp_path / "backup" / "personal" / "current"
        current.mkdir(parents=True)
        (current / "file1.txt").write_bytes(b"0123456789")

        resp = await authenticated_client.get(
            "/api/v1/local-backup/download",
            params={"path": "/personal/file1.txt"},
            headers={"Range": "bytes=2-5"},
        )
        assert resp.status_code == 206
        assert resp.content == b"2345"
        assert resp.headers["content-range"] == "bytes 2-5/10"


class TestMediaBackupRoutes:
    """Route-level tests for the media-library backup endpoints (2026-07-16 audit
    finding #1). local_backup.py's own test_local_backup_media.py covers the actual
    sync/restore engine in depth; these cover auth, state transitions, and wiring."""

    @pytest.mark.asyncio
    async def test_status_includes_media_backup_block_default_disabled(
        self, authenticated_client: AsyncClient,
    ):
        resp = await authenticated_client.get("/api/v1/local-backup/status")
        assert resp.status_code == 200
        media = resp.json()["mediaBackup"]
        assert media["enabled"] is False
        assert media["lastSyncAt"] is None

    @pytest.mark.asyncio
    async def test_enable_requires_admin(self, client: AsyncClient):
        resp = await client.post("/api/v1/local-backup/media/enable")
        assert resp.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_enable_sets_enabled_and_returns_capacity_warning_when_backup_too_small(
        self, authenticated_client: AsyncClient,
    ):
        with patch(
            "app.routes.local_backup_routes.disk_usage",
            side_effect=[
                type("U", (), {"total": 0, "used": 500 * 1024**3, "free": 0})(),  # primary
                type("U", (), {"total": 10 * 1024**3, "used": 0, "free": 10 * 1024**3})(),  # backup
            ],
        ), patch("app.local_backup.sync_media_library", new=AsyncMock(return_value={"status": "ok"})):
            resp = await authenticated_client.post("/api/v1/local-backup/media/enable")

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "enabled"
        assert body["capacityWarning"] is not None
        assert "smaller" in body["capacityWarning"]

        status_resp = await authenticated_client.get("/api/v1/local-backup/status")
        assert status_resp.json()["mediaBackup"]["enabled"] is True

    @pytest.mark.asyncio
    async def test_disable_clears_enabled_flag(self, authenticated_client: AsyncClient):
        with patch("app.local_backup.sync_media_library", new=AsyncMock(return_value={"status": "ok"})):
            await authenticated_client.post("/api/v1/local-backup/media/enable")
        resp = await authenticated_client.post("/api/v1/local-backup/media/disable")
        assert resp.status_code == 200

        status_resp = await authenticated_client.get("/api/v1/local-backup/status")
        assert status_resp.json()["mediaBackup"]["enabled"] is False

    @pytest.mark.asyncio
    async def test_media_sync_now_rejects_when_not_enabled(self, authenticated_client: AsyncClient):
        resp = await authenticated_client.post("/api/v1/local-backup/media/sync-now")
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_media_sync_now_starts_when_enabled(self, authenticated_client: AsyncClient):
        with patch("app.local_backup.sync_media_library", new=AsyncMock(return_value={"status": "ok"})):
            await authenticated_client.post("/api/v1/local-backup/media/enable")
            resp = await authenticated_client.post("/api/v1/local-backup/media/sync-now")

        assert resp.status_code == 202
        assert resp.json()["status"] == "started"

    @pytest.mark.asyncio
    async def test_restore_requires_admin(self, client: AsyncClient):
        resp = await client.post("/api/v1/local-backup/restore")
        assert resp.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_restore_requires_a_mounted_drive(self, authenticated_client: AsyncClient):
        resp = await authenticated_client.post("/api/v1/local-backup/restore")
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_restore_starts_a_pollable_job_when_drive_mounted(
        self, authenticated_client: AsyncClient,
    ):
        with patch(
            "app.local_backup.get_drive_state",
            new=AsyncMock(return_value={"activeDevice": "/dev/fake1"}),
        ), patch(
            "app.local_backup.restore_media_library",
            new=AsyncMock(return_value={"status": "ok", "restored": 2, "failed": 0}),
        ):
            resp = await authenticated_client.post("/api/v1/local-backup/restore")

        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "started"
        assert "jobId" in body
