"""
Tests for the Telegram download stall-watchdog in bot_core._download_to_path.

This is the direct fix for a real incident: a download hung with zero byte
progress for 10+ minutes past its own configured timeout, with no
self-recovery, requiring a manual service restart. These tests simulate
that exact failure mode (a transfer that never writes any bytes) and verify
it's now caught and cleaned up automatically, plus that a normal transfer
completes untouched.

No real Telegram connection or data_dir isolation needed — _download_to_path
only touches the filesystem path it's given and a mocked bot.
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.telegram.bot_core import DownloadStallError, InsufficientDiskSpaceError, _download_to_path
from app.config import settings


def _make_bot(download_behavior):
    """Build a mock bot whose get_file().download_to_drive() runs *download_behavior*."""
    telegram_file = MagicMock()
    telegram_file.download_to_drive = AsyncMock(side_effect=download_behavior)
    # Unset (None), matching a real telegram.File when Telegram doesn't report a size —
    # an unconfigured MagicMock here would make the pre-flight disk-space check compare
    # against a Mock instead of an int.
    telegram_file.file_size = None
    bot = MagicMock()
    bot.get_file = AsyncMock(return_value=telegram_file)
    return bot


@pytest.mark.asyncio
async def test_download_completes_normally(tmp_path, monkeypatch):
    """A well-behaved download that writes its bytes promptly completes and is
    atomically renamed from the .uploading temp to the final path."""
    monkeypatch.setattr(settings, "telegram_stall_timeout", 2)
    dest = tmp_path / "video.mp4"

    async def _write_then_finish(custom_path):
        Path(custom_path).write_bytes(b"real video bytes")

    bot = _make_bot(_write_then_finish)
    result = await _download_to_path(bot, "file-1", dest)

    assert result == dest
    assert dest.read_bytes() == b"real video bytes"
    assert not dest.with_name(dest.name + ".uploading").exists()


@pytest.mark.asyncio
async def test_stalled_download_is_aborted_and_cleaned_up(tmp_path, monkeypatch):
    """A download that writes nothing and never returns (simulating the exact
    incident — a connection alive at the socket level but making zero actual
    progress) is aborted once it exceeds telegram_stall_timeout, and leaves no
    partial file behind."""
    monkeypatch.setattr(settings, "telegram_stall_timeout", 1)

    from app.telegram import bot_core
    monkeypatch.setattr(bot_core, "_DOWNLOAD_STALL_POLL_INTERVAL_SECONDS", 0.2)

    dest = tmp_path / "stuck.mp4"

    async def _hang_forever(custom_path):
        # Never writes to custom_path — this is the exact stuck-download shape.
        await asyncio.sleep(3600)

    bot = _make_bot(_hang_forever)

    with pytest.raises(DownloadStallError):
        await _download_to_path(bot, "file-2", dest)

    assert not dest.exists()
    assert not dest.with_name(dest.name + ".uploading").exists()


@pytest.mark.asyncio
async def test_download_progressing_slowly_is_not_killed(tmp_path, monkeypatch):
    """A slow-but-genuinely-progressing download (bytes keep landing on disk,
    just not quickly) must NOT be killed — the watchdog bounds the gap between
    progress, not total transfer time."""
    monkeypatch.setattr(settings, "telegram_stall_timeout", 1)
    from app.telegram import bot_core
    monkeypatch.setattr(bot_core, "_DOWNLOAD_STALL_POLL_INTERVAL_SECONDS", 0.2)

    dest = tmp_path / "slow.mp4"

    async def _trickle(custom_path):
        p = Path(custom_path)
        for chunk in (b"a", b"b", b"c"):
            with open(p, "ab") as f:
                f.write(chunk)
            await asyncio.sleep(0.3)  # less than the stall timeout each step

    bot = _make_bot(_trickle)
    result = await _download_to_path(bot, "file-3", dest)

    assert result == dest
    assert dest.read_bytes() == b"abc"


@pytest.mark.asyncio
async def test_download_rejected_upfront_when_not_enough_disk_space(tmp_path, monkeypatch):
    """A file whose reported size doesn't fit in the destination's free space (plus margin)
    is rejected before any bytes transfer, rather than failing partway through a multi-GB
    transfer — found live 2026-07-15 auditing this path for large movie/anime downloads."""
    from app.telegram import bot_core

    async def _never_called(custom_path):
        raise AssertionError("download_to_drive must not be called when space check fails")

    bot = _make_bot(_never_called)
    telegram_file = await bot.get_file("file-4")
    telegram_file.file_size = 5 * 1024 * 1024 * 1024  # 5 GB — bigger than any test tmpfs

    monkeypatch.setattr(
        bot_core.shutil, "disk_usage",
        lambda path: type("Usage", (), {"free": 1 * 1024 * 1024 * 1024})(),  # 1 GB free
    )

    dest = tmp_path / "huge_movie.mp4"
    with pytest.raises(InsufficientDiskSpaceError) as exc_info:
        await _download_to_path(bot, "file-4", dest)

    assert exc_info.value.needed == 5 * 1024 * 1024 * 1024
    assert not dest.exists()
    assert not dest.with_name(dest.name + ".uploading").exists()


@pytest.mark.asyncio
async def test_download_proceeds_when_file_size_unknown(tmp_path):
    """Telegram doesn't always report file_size (e.g. some document types) — the disk-space
    check must not block a download just because the size is unknown."""
    dest = tmp_path / "unknown_size.mp4"

    async def _write_then_finish(custom_path):
        Path(custom_path).write_bytes(b"bytes")

    bot = _make_bot(_write_then_finish)
    result = await _download_to_path(bot, "file-5", dest)

    assert result == dest
    assert dest.read_bytes() == b"bytes"
