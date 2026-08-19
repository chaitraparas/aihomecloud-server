"""
Tests for local_backup.py — the protected-folder snapshot sync engine.

Runs against real temp directories (not mocked filesystem) with a stubbed
run_command so rsync semantics (--link-dest, --delete) can be asserted on the
actual command built, without needing a real rsync binary to be exercised end
to end here — the file-level effects (symlink repointing, pruning) ARE real,
since those are plain pathlib operations this module owns directly.
"""

import shutil
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app import local_backup
from app.config import settings


@pytest.fixture(autouse=True)
def _reset_lock():
    # Guard against a previous test leaving the module-level lock held (e.g. an
    # assertion failure inside an `async with` block) from poisoning the next test.
    yield
    if local_backup._sync_lock.locked():
        local_backup._sync_lock.release()


class TestSlugify:
    def test_nested_path(self):
        assert local_backup._slugify("/personal/Documents") == "personal_Documents"

    def test_root_path(self):
        assert local_backup._slugify("/") == "root"

    def test_strips_unsafe_characters(self):
        assert local_backup._slugify("/a b/c!d") == "a_b_c_d"


class TestSyncOneFolder:
    @pytest.mark.asyncio
    async def test_missing_source_folder_records_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "nas_root", tmp_path / "nas")
        monkeypatch.setattr(settings, "backup_root", tmp_path / "backup")
        (tmp_path / "nas").mkdir()

        entry = {"path": "/personal/Gone", "label": "Gone"}
        result = await local_backup._sync_one_folder(entry)

        assert result["lastSyncStatus"] == "error"
        assert "no longer exists" in result["lastSyncError"]

    @pytest.mark.asyncio
    async def test_successful_sync_creates_snapshot_and_current_symlink(self, tmp_path, monkeypatch):
        nas_root = tmp_path / "nas"
        backup_root = tmp_path / "backup"
        monkeypatch.setattr(settings, "nas_root", nas_root)
        monkeypatch.setattr(settings, "backup_root", backup_root)
        src = nas_root / "personal" / "Documents"
        src.mkdir(parents=True)

        async def fake_run_command(cmd, timeout=30):
            # Simulate rsync actually creating the destination directory.
            dest = cmd[-1].rstrip("/")
            from pathlib import Path
            Path(dest).mkdir(parents=True, exist_ok=True)
            return 0, "", ""

        with patch("app.local_backup.run_command", side_effect=fake_run_command):
            entry = {"path": "/personal/Documents", "label": "Documents"}
            result = await local_backup._sync_one_folder(entry)

        assert result["lastSyncStatus"] == "ok"
        assert result["lastSyncError"] is None
        current = backup_root / "personal_Documents" / "current"
        assert current.is_symlink()
        assert current.resolve().is_dir()

    @pytest.mark.asyncio
    async def test_rsync_failure_records_error_and_cleans_up_snapshot(self, tmp_path, monkeypatch):
        nas_root = tmp_path / "nas"
        backup_root = tmp_path / "backup"
        monkeypatch.setattr(settings, "nas_root", nas_root)
        monkeypatch.setattr(settings, "backup_root", backup_root)
        src = nas_root / "personal" / "Documents"
        src.mkdir(parents=True)

        with patch("app.local_backup.run_command", new_callable=AsyncMock, return_value=(1, "", "rsync: boom")):
            entry = {"path": "/personal/Documents", "label": "Documents"}
            result = await local_backup._sync_one_folder(entry)

        assert result["lastSyncStatus"] == "error"
        assert "boom" in result["lastSyncError"]
        assert not (backup_root / "personal_Documents" / "current").exists()

    @pytest.mark.asyncio
    async def test_second_run_uses_link_dest_of_current_snapshot(self, tmp_path, monkeypatch):
        nas_root = tmp_path / "nas"
        backup_root = tmp_path / "backup"
        monkeypatch.setattr(settings, "nas_root", nas_root)
        monkeypatch.setattr(settings, "backup_root", backup_root)
        src = nas_root / "personal" / "Documents"
        src.mkdir(parents=True)

        seen_cmds = []

        async def fake_run_command(cmd, timeout=30):
            seen_cmds.append(cmd)
            from pathlib import Path
            Path(cmd[-1].rstrip("/")).mkdir(parents=True, exist_ok=True)
            return 0, "", ""

        entry = {"path": "/personal/Documents", "label": "Documents"}
        with patch("app.local_backup.run_command", side_effect=fake_run_command):
            await local_backup._sync_one_folder(entry)
            await local_backup._sync_one_folder(entry)

        assert len(seen_cmds) == 2
        assert not any(a.startswith("--link-dest=") for a in seen_cmds[0])
        assert any(a.startswith("--link-dest=") for a in seen_cmds[1])


class TestPruneOldSnapshots:
    def test_keeps_only_the_most_recent_n(self, tmp_path):
        snapshots_dir = tmp_path / "snapshots"
        snapshots_dir.mkdir()
        for name in ["20260101T000000Z", "20260102T000000Z", "20260103T000000Z", "20260104T000000Z"]:
            (snapshots_dir / name).mkdir()

        local_backup._prune_old_snapshots(snapshots_dir, keep=3)

        remaining = sorted(p.name for p in snapshots_dir.iterdir())
        assert remaining == ["20260102T000000Z", "20260103T000000Z", "20260104T000000Z"]

    def test_noop_when_at_or_under_retention(self, tmp_path):
        snapshots_dir = tmp_path / "snapshots"
        snapshots_dir.mkdir()
        (snapshots_dir / "20260101T000000Z").mkdir()

        local_backup._prune_old_snapshots(snapshots_dir, keep=3)

        assert len(list(snapshots_dir.iterdir())) == 1


class TestFindOwningProtectedFolder:
    def test_exact_match(self):
        folders = [{"path": "/personal/Documents"}]
        assert local_backup._find_owning_protected_folder(folders, "/personal/Documents") == folders[0]

    def test_nested_subpath_matches_ancestor(self):
        folders = [{"path": "/personal/Documents"}]
        owner = local_backup._find_owning_protected_folder(folders, "/personal/Documents/Reports/2026")
        assert owner == folders[0]

    def test_unrelated_path_does_not_match(self):
        folders = [{"path": "/personal/Documents"}]
        assert local_backup._find_owning_protected_folder(folders, "/personal/Photos") is None

    def test_sibling_prefix_does_not_falsely_match(self):
        # "/personal/Doc" must not be treated as an ancestor of a browse into
        # "/personal/Documents2" just because it's a string prefix.
        folders = [{"path": "/personal/Doc"}]
        assert local_backup._find_owning_protected_folder(folders, "/personal/Documents2") is None

    def test_picks_longest_matching_ancestor(self):
        folders = [{"path": "/personal"}, {"path": "/personal/Documents"}]
        owner = local_backup._find_owning_protected_folder(folders, "/personal/Documents/Reports")
        assert owner["path"] == "/personal/Documents"


class TestResolveBrowseTarget:
    @pytest.mark.asyncio
    async def test_not_protected_returns_none_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "data_dir", tmp_path)
        from app import store
        store._cache.clear()

        owner, target = await local_backup.resolve_browse_target("/personal/Documents")
        assert owner is None
        assert target is None

    @pytest.mark.asyncio
    async def test_root_of_protected_folder_resolves_to_current(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "data_dir", tmp_path)
        monkeypatch.setattr(settings, "backup_root", tmp_path / "backup")
        from app import store
        store._cache.clear()
        await store.set_value(local_backup.PROTECTED_FOLDERS_KEY, [{"path": "/personal/Documents"}])

        current = tmp_path / "backup" / "personal_Documents" / "current"
        current.mkdir(parents=True)

        owner, target = await local_backup.resolve_browse_target("/personal/Documents")
        assert owner["path"] == "/personal/Documents"
        assert target == current.resolve()

    @pytest.mark.asyncio
    async def test_nested_path_resolves_inside_current(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "data_dir", tmp_path)
        monkeypatch.setattr(settings, "backup_root", tmp_path / "backup")
        from app import store
        store._cache.clear()
        await store.set_value(local_backup.PROTECTED_FOLDERS_KEY, [{"path": "/personal/Documents"}])

        current = tmp_path / "backup" / "personal_Documents" / "current"
        (current / "Reports").mkdir(parents=True)

        owner, target = await local_backup.resolve_browse_target("/personal/Documents/Reports")
        assert owner["path"] == "/personal/Documents"
        assert target == (current / "Reports").resolve()

    @pytest.mark.asyncio
    async def test_traversal_attempt_returns_owner_but_none_target(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "data_dir", tmp_path)
        monkeypatch.setattr(settings, "backup_root", tmp_path / "backup")
        from app import store
        store._cache.clear()
        await store.set_value(local_backup.PROTECTED_FOLDERS_KEY, [{"path": "/personal/Documents"}])
        (tmp_path / "backup" / "personal_Documents" / "current").mkdir(parents=True)

        owner, target = await local_backup.resolve_browse_target("/personal/Documents/../../../etc")
        assert owner is not None
        assert target is None


class TestSyncProtectedFolders:
    @pytest.mark.asyncio
    async def test_no_drive_mounted_short_circuits(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "data_dir", tmp_path)
        from app import store
        store._cache.clear()

        result = await local_backup.sync_protected_folders()

        assert result["status"] == "no_drive_mounted"
        assert result["synced"] == 0

    @pytest.mark.asyncio
    async def test_already_syncing_short_circuits(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "data_dir", tmp_path)
        from app import store
        store._cache.clear()

        await local_backup._sync_lock.acquire()
        try:
            result = await local_backup.sync_protected_folders()
        finally:
            local_backup._sync_lock.release()

        assert result["status"] == "already_syncing"


class TestTryAutoRemount:
    """A reboot leaves the backup drive genuinely unmounted at the OS level (never added to
    fstab), but the stored drive state keeps claiming it's mounted with nothing to correct it
    -- found live 2026-07-15 on the Cubie A5E. These pin the fix: remount on startup if the
    saved device still exists, clear stale state if it doesn't, never blind-scan for a
    replacement (unlike primary storage)."""

    @pytest.mark.asyncio
    async def test_noop_when_no_saved_device(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "data_dir", tmp_path)
        from app import store
        store._cache.clear()

        with patch("app.routes.storage_helpers.mount_backup_device") as mock_mount:
            await local_backup.try_auto_remount()

        mock_mount.assert_not_called()

    @pytest.mark.asyncio
    async def test_clears_stale_state_when_device_no_longer_exists(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "data_dir", tmp_path)
        from app import store
        store._cache.clear()
        await local_backup.save_drive_state({"activeDevice": "/dev/sdz1-does-not-exist"})

        with patch("app.routes.storage_helpers.mount_backup_device") as mock_mount:
            await local_backup.try_auto_remount()

        mock_mount.assert_not_called()
        assert await local_backup.get_drive_state() == {}

    @pytest.mark.asyncio
    async def test_remounts_when_saved_device_still_exists(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "data_dir", tmp_path)
        from app import store
        store._cache.clear()
        real_device = tmp_path / "fake-dev-sda1"
        real_device.write_text("stand-in for a block device node")
        await local_backup.save_drive_state({"activeDevice": str(real_device)})

        with patch(
            "app.routes.storage_helpers.mount_backup_device",
            new=AsyncMock(return_value=(0, "", "")),
        ) as mock_mount:
            await local_backup.try_auto_remount()

        mock_mount.assert_awaited_once_with(str(real_device))

    @pytest.mark.asyncio
    async def test_leaves_state_untouched_when_remount_fails(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "data_dir", tmp_path)
        from app import store
        store._cache.clear()
        real_device = tmp_path / "fake-dev-sda1"
        real_device.write_text("stand-in for a block device node")
        await local_backup.save_drive_state({"activeDevice": str(real_device)})

        with patch(
            "app.routes.storage_helpers.mount_backup_device",
            new=AsyncMock(return_value=(1, "", "mount failed")),
        ):
            await local_backup.try_auto_remount()

        # Not cleared -- a transient mount failure (e.g. USB power blip) shouldn't discard the
        # user's configuration the way a genuinely missing device does.
        assert (await local_backup.get_drive_state())["activeDevice"] == str(real_device)
