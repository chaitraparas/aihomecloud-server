"""
Tests for local_backup.py's media-library mode (2026-07-16, full-repo audit finding #1:
the media library itself had exactly one copy). Runs against real temp directories with a
stubbed run_command that performs a REAL additive copy (mirroring how rsync -a without
--delete actually behaves) so the additive/no-delete guarantee is genuinely exercised, not
just asserted against the command string -- see test_local_backup.py's own docstring for
why this project prefers real file-level effects over a fully mocked filesystem.
"""

import shutil
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app import local_backup
from app.config import settings


@pytest.fixture(autouse=True)
def _reset_lock():
    yield
    if local_backup._sync_lock.locked():
        local_backup._sync_lock.release()


def _fake_rsync_copy(cmd, timeout=30):
    """Stand-in for `nice -n 10 ionice -c3 rsync -a [--ignore-existing] <src>/ <dest>/`.
    Performs a real additive copy: never removes anything already at dest; if
    --ignore-existing is present, never overwrites an existing dest file either."""
    src, dest = Path(cmd[-2].rstrip("/")), Path(cmd[-1].rstrip("/"))
    ignore_existing = "--ignore-existing" in cmd
    dest.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        for item in src.rglob("*"):
            if item.is_dir():
                continue
            rel = item.relative_to(src)
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if ignore_existing and target.exists():
                continue
            shutil.copy2(item, target)
    return 0, "", ""


async def _fake_rsync_copy_async(cmd, timeout=30):
    return _fake_rsync_copy(cmd, timeout)


def _setup_roots(tmp_path, monkeypatch):
    from app import store

    nas_root = tmp_path / "nas"
    backup_root = tmp_path / "backup"
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "nas_root", nas_root)
    monkeypatch.setattr(settings, "backup_root", backup_root)
    store._cache.clear()
    (nas_root / "family" / "Photos").mkdir(parents=True)
    (nas_root / "entertainment").mkdir(parents=True)
    (nas_root / "personal" / "alice" / "Photos").mkdir(parents=True)
    monkeypatch.setattr(settings, "family_dir", "family")
    monkeypatch.setattr(settings, "entertainment_dir", "entertainment")
    monkeypatch.setattr(settings, "personal_base", "personal")
    return nas_root, backup_root


class TestSyncMediaLibraryGuards:
    @pytest.mark.asyncio
    async def test_noop_when_disabled(self, tmp_path, monkeypatch):
        _setup_roots(tmp_path, monkeypatch)
        await local_backup.set_media_enabled(False)
        await local_backup.save_drive_state({"activeDevice": "/dev/fake1"})

        with patch("app.local_backup.run_command", new_callable=AsyncMock) as mock_run:
            result = await local_backup.sync_media_library()

        assert result["status"] == "disabled"
        mock_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_noop_when_no_drive_mounted(self, tmp_path, monkeypatch):
        _setup_roots(tmp_path, monkeypatch)
        await local_backup.set_media_enabled(True)
        await local_backup.save_drive_state({})

        with patch("app.local_backup.run_command", new_callable=AsyncMock) as mock_run:
            result = await local_backup.sync_media_library()

        assert result["status"] == "no_drive_mounted"
        mock_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_refuses_to_sync_when_backup_drive_nearly_full(self, tmp_path, monkeypatch):
        _setup_roots(tmp_path, monkeypatch)
        settings.backup_root.mkdir(parents=True, exist_ok=True)
        await local_backup.set_media_enabled(True)
        await local_backup.save_drive_state({"activeDevice": "/dev/fake1"})

        fake_usage = type("Usage", (), {"total": 10**9, "used": 10**9 - 100, "free": 100})()
        with patch("app.local_backup.shutil.disk_usage", return_value=fake_usage), patch(
            "app.local_backup.run_command", new_callable=AsyncMock,
        ) as mock_run:
            result = await local_backup.sync_media_library()

        assert result["status"] == "low_space"
        mock_run.assert_not_called()
        state = await local_backup.get_media_state()
        assert state["lastSyncStatus"] == "error"


class TestSyncMediaLibraryCopiesFiles:
    @pytest.mark.asyncio
    async def test_copies_all_three_scopes(self, tmp_path, monkeypatch):
        nas_root, backup_root = _setup_roots(tmp_path, monkeypatch)
        (nas_root / "family" / "Photos" / "shared.jpg").write_bytes(b"family photo")
        (nas_root / "entertainment" / "movie.mp4").write_bytes(b"a movie")
        (nas_root / "personal" / "alice" / "Photos" / "mine.jpg").write_bytes(b"alice's photo")

        await local_backup.set_media_enabled(True)
        await local_backup.save_drive_state({"activeDevice": "/dev/fake1"})

        with patch("app.local_backup.run_command", side_effect=_fake_rsync_copy_async):
            result = await local_backup.sync_media_library()

        assert result["status"] == "ok"
        assert result["failed"] == 0
        assert (backup_root / "media" / "family" / "Photos" / "shared.jpg").read_bytes() == b"family photo"
        assert (backup_root / "media" / "entertainment" / "movie.mp4").read_bytes() == b"a movie"
        assert (
            backup_root / "media" / "personal" / "alice" / "Photos" / "mine.jpg"
        ).read_bytes() == b"alice's photo"

        state = await local_backup.get_media_state()
        assert state["lastSyncStatus"] == "ok"

    @pytest.mark.asyncio
    async def test_deleting_a_file_on_primary_does_not_remove_it_from_backup(self, tmp_path, monkeypatch):
        # The core additive-mirror guarantee: media sync must NEVER pass --delete.
        nas_root, backup_root = _setup_roots(tmp_path, monkeypatch)
        photo = nas_root / "family" / "Photos" / "keepsake.jpg"
        photo.write_bytes(b"an irreplaceable family photo")

        await local_backup.set_media_enabled(True)
        await local_backup.save_drive_state({"activeDevice": "/dev/fake1"})

        with patch("app.local_backup.run_command", side_effect=_fake_rsync_copy_async):
            await local_backup.sync_media_library()

        backup_copy = backup_root / "media" / "family" / "Photos" / "keepsake.jpg"
        assert backup_copy.read_bytes() == b"an irreplaceable family photo"

        # Simulate the exact incident this feature exists for: the file is gone from primary.
        photo.unlink()
        with patch("app.local_backup.run_command", side_effect=_fake_rsync_copy_async):
            await local_backup.sync_media_library()

        assert backup_copy.exists(), "an additive mirror must never delete from backup"
        assert backup_copy.read_bytes() == b"an irreplaceable family photo"

    @pytest.mark.asyncio
    async def test_rsync_command_never_includes_delete_flag(self, tmp_path, monkeypatch):
        _setup_roots(tmp_path, monkeypatch)
        (settings.nas_root / "family" / "Photos" / "a.jpg").write_bytes(b"x")
        await local_backup.set_media_enabled(True)
        await local_backup.save_drive_state({"activeDevice": "/dev/fake1"})

        captured_cmds = []

        async def capture(cmd, timeout=30):
            captured_cmds.append(cmd)
            return await _fake_rsync_copy_async(cmd, timeout)

        with patch("app.local_backup.run_command", side_effect=capture):
            await local_backup.sync_media_library()

        assert captured_cmds, "expected at least one rsync invocation"
        for cmd in captured_cmds:
            assert "--delete" not in cmd
            assert "--checksum" not in cmd
            assert "-H" not in cmd


class TestRestoreMediaLibrary:
    @pytest.mark.asyncio
    async def test_noop_when_no_drive_mounted(self, tmp_path, monkeypatch):
        _setup_roots(tmp_path, monkeypatch)
        await local_backup.save_drive_state({})

        with patch("app.local_backup.run_command", new_callable=AsyncMock) as mock_run:
            result = await local_backup.restore_media_library()

        assert result["status"] == "no_drive_mounted"
        mock_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_restore_never_overwrites_a_file_already_on_primary(self, tmp_path, monkeypatch):
        nas_root, backup_root = _setup_roots(tmp_path, monkeypatch)
        # Backup has an OLD version; primary already has a NEWER version of the same file.
        backup_photo = backup_root / "media" / "family" / "Photos" / "same_name.jpg"
        backup_photo.parent.mkdir(parents=True)
        backup_photo.write_bytes(b"stale backup content")
        primary_photo = nas_root / "family" / "Photos" / "same_name.jpg"
        primary_photo.write_bytes(b"current primary content -- must not be clobbered")

        await local_backup.save_drive_state({"activeDevice": "/dev/fake1"})

        with patch("app.local_backup.run_command", side_effect=_fake_rsync_copy_async), patch(
            "app.media_reconciler.reconcile_once", new_callable=AsyncMock,
        ):
            result = await local_backup.restore_media_library()

        assert result["status"] == "ok"
        assert primary_photo.read_bytes() == b"current primary content -- must not be clobbered", (
            "--ignore-existing must never overwrite a file already on primary"
        )

    @pytest.mark.asyncio
    async def test_restore_fills_a_genuine_gap_on_primary(self, tmp_path, monkeypatch):
        nas_root, backup_root = _setup_roots(tmp_path, monkeypatch)
        backup_photo = backup_root / "media" / "family" / "Photos" / "lost.jpg"
        backup_photo.parent.mkdir(parents=True)
        backup_photo.write_bytes(b"recovered from backup")

        await local_backup.save_drive_state({"activeDevice": "/dev/fake1"})

        with patch("app.local_backup.run_command", side_effect=_fake_rsync_copy_async), patch(
            "app.media_reconciler.reconcile_once", new_callable=AsyncMock,
        ):
            result = await local_backup.restore_media_library()

        assert result["status"] == "ok"
        restored = nas_root / "family" / "Photos" / "lost.jpg"
        assert restored.exists()
        assert restored.read_bytes() == b"recovered from backup"

    @pytest.mark.asyncio
    async def test_restore_triggers_an_incremental_reconcile(self, tmp_path, monkeypatch):
        _setup_roots(tmp_path, monkeypatch)
        await local_backup.save_drive_state({"activeDevice": "/dev/fake1"})

        with patch("app.local_backup.run_command", side_effect=_fake_rsync_copy_async), patch(
            "app.media_reconciler.reconcile_once", new_callable=AsyncMock,
        ) as mock_reconcile:
            await local_backup.restore_media_library()

        mock_reconcile.assert_awaited_once()
