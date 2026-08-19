"""
Serialises destructive operations against the same physical disk.

Why this exists: `POST /storage/format` and `POST /storage/smart-activate` both dispatch a
background job that runs `mkfs`/`sgdisk`, and neither recorded anywhere that a device already
had one in flight. Two rapid calls — a double-tap, a stuck UI retry, two admins — could spawn
two concurrent `mkfs.ext4` processes against the same block device. Found in the 2026-07-30
bug hunt (storage findings 5 and 6) and deferred there as needing a dedicated pass rather than
a rushed lock.

**Locks are per physical disk, not per partition.** Formatting `/dev/sda` while something else
mounts `/dev/sda1` is the same conflict as two formats: the partition table is being rewritten
underneath a live filesystem. Normalising to the parent disk is what makes the lock actually
protect the hardware rather than a string.

**In-process only, and that is sufficient here — but only because the backend runs as a single
uvicorn process** (`app/main.py` calls `uvicorn.run` with no `workers` argument). If this ever
becomes multi-worker or multi-host, this stops being a lock and becomes a comment: it would
need a filesystem or database lock instead (`flock` on the device path is the usual answer).
There is no runtime assertion for this — uvicorn does not expose its worker count to the app —
so it is stated here and in backend/CLAUDE.md instead of pretended to be enforced.
"""

from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

logger = logging.getLogger("aihomecloud.device_locks")

# /dev/sda1 -> /dev/sda ; /dev/nvme0n1p3 -> /dev/nvme0n1 ; /dev/mmcblk0p2 -> /dev/mmcblk0
_NVME_MMC_PART = re.compile(r"^(/dev/(?:nvme\d+n\d+|mmcblk\d+))p\d+$")
# Only the families where a trailing number really means "partition". A bare
# `/dev/[a-z]+\d+` pattern is wrong: /dev/mmcblk0 and /dev/loop0 are whole disks whose
# names simply end in a digit, and stripping it would merge unrelated devices onto one
# lock. Caught by the tests below.
_SCSI_PART = re.compile(r"^(/dev/(?:sd|hd|vd|xvd)[a-z]+)\d+$")

_busy: dict[str, str] = {}
_guard = asyncio.Lock()


class DeviceBusyError(RuntimeError):
    """Raised when another destructive operation already holds this disk."""

    def __init__(self, device: str, holder: str):
        self.device = device
        self.holder = holder
        super().__init__(f"{device} is busy: {holder}")


def base_disk(device: str) -> str:
    """
    The parent disk for a partition path, or the input unchanged if it is already a disk.

    Deliberately conservative: anything unrecognised is returned as-is rather than guessed at.
    A wrong normalisation would either merge two unrelated disks into one lock (blocking valid
    work) or split one disk into two (defeating the lock entirely), and of those, failing to
    normalise is the safer error — it only ever under-merges.
    """
    if not device:
        return device
    m = _NVME_MMC_PART.match(device)
    if m:
        return m.group(1)
    m = _SCSI_PART.match(device)
    if m:
        return m.group(1)
    return device


async def acquire(device: str, holder: str) -> None:
    """Claim `device` (normalised to its parent disk). Raises [DeviceBusyError] if taken."""
    disk = base_disk(device)
    async with _guard:
        existing = _busy.get(disk)
        if existing is not None:
            raise DeviceBusyError(disk, existing)
        _busy[disk] = holder
        logger.info("device_lock_acquired disk=%s holder=%s", disk, holder)


async def release(device: str) -> None:
    """Release `device`. Safe to call when not held — releasing twice must never raise."""
    disk = base_disk(device)
    async with _guard:
        holder = _busy.pop(disk, None)
    if holder is not None:
        logger.info("device_lock_released disk=%s holder=%s", disk, holder)


async def holder_of(device: str) -> Optional[str]:
    disk = base_disk(device)
    async with _guard:
        return _busy.get(disk)


@asynccontextmanager
async def device_lock(device: str, holder: str) -> AsyncIterator[None]:
    """
    Hold `device` for the duration of the block.

    Release is in a `finally`, so a job that raises, times out or is cancelled still frees the
    disk. Without that, one crashed format would make the device permanently unformattable
    until the service restarted — a worse failure than the race being prevented.
    """
    await acquire(device, holder)
    try:
        yield
    finally:
        await release(device)


def _reset_for_tests() -> None:
    _busy.clear()
