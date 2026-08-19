"""
Storage helper functions — device classification, lsblk parsing, mount/unmount logic.

Split from storage_routes.py for better RAG chunking and code navigation.
Used internally by storage_routes.py endpoint handlers.
"""

import json
import logging
import time
from typing import List, Optional, Tuple

from fastapi import HTTPException

from ..config import settings
from ..models import StorageDevice
from .. import store
from ..subprocess_runner import run_command

logger = logging.getLogger("aihomecloud.storage")

# ── Host-namespace mount/umount escape ──────────────────────────────────────
#
# The aihomecloud.service unit runs under ProtectSystem=strict + ReadWritePaths=
# /srv/nas, which systemd implements via a private mount namespace: /srv/nas is
# bind-mounted in as a SLAVE of the host's mount (propagation is one-way,
# host -> sandbox only). A mount/umount syscall made directly by this process
# only affects its own private namespace view and can never change the real,
# system-wide mount table — confirmed via /proc/<pid>/mountinfo showing
# `master:N` on the sandboxed side vs `shared:N` on the host side. Storage
# eject/mount therefore cannot be done as a direct subprocess call; instead we
# ask systemd (PID 1, which lives in the host's namespace) to run two small,
# unsandboxed oneshot units on our behalf via `systemctl start` — authorized
# for the aihomecloud user via a scoped polkit rule, not full sudo.


async def mount_nas_device(device_path: str) -> Tuple[int, str, str]:
    """Mount *device_path* at the NAS root, in the HOST mount namespace."""
    rc_esc, escaped, err_esc = await run_command(["systemd-escape", "--path", device_path])
    if rc_esc != 0:
        return rc_esc, "", f"systemd-escape failed: {err_esc}"
    unit = f"ahc-mount@{escaped.strip()}.service"
    return await run_command(["systemctl", "start", unit], timeout=30)


async def unmount_nas_device() -> Tuple[int, str, str]:
    """Unmount the NAS root, in the HOST mount namespace."""
    return await run_command(["systemctl", "start", "ahc-umount.service"], timeout=30)


async def mount_backup_device(device_path: str) -> Tuple[int, str, str]:
    """Mount *device_path* at settings.backup_root (local_backup.py's secondary
    drive), in the HOST mount namespace. Same escape-hatch shape as
    mount_nas_device — a separate unit template because ahc-mount@.service
    hardcodes /srv/nas as its target, not because the underlying script differs."""
    rc_esc, escaped, err_esc = await run_command(["systemd-escape", "--path", device_path])
    if rc_esc != 0:
        return rc_esc, "", f"systemd-escape failed: {err_esc}"
    unit = f"ahc-mount-backup@{escaped.strip()}.service"
    return await run_command(["systemctl", "start", unit], timeout=30)


async def unmount_backup_device() -> Tuple[int, str, str]:
    """Unmount settings.backup_root, in the HOST mount namespace."""
    return await run_command(["systemctl", "start", "ahc-umount-backup.service"], timeout=30)


async def partition_and_format_device(disk_path: str) -> Tuple[int, str, str]:
    """Wipe *disk_path*, create one GPT partition spanning the whole disk, and
    mkfs.ext4 it — in the HOST mount/device namespace, same escape-hatch as
    mount_nas_device above.

    A direct `sgdisk`/`mkfs.ext4` subprocess call from inside the sandboxed
    aihomecloud.service cannot open the raw block device at all: found live
    2026-08-02 on a genuinely fresh install (first real Activate-a-drive
    attempt this app has ever done end-to-end) — sgdisk failed with "Problem
    opening /dev/nvme0n1 for reading! Error is 13", and prefixing `sudo` (as
    the neighboring udevadm calls in this file already do) doesn't fix it
    either: NoNewPrivileges=yes on the service unit blocks sudo's setuid
    escalation at the kernel level regardless of sudoers content, so every
    `sudo ...` subprocess call from this service has silently never worked.
    Routes through the same ahc-*@ polkit-authorized oneshot-unit pattern
    mount/umount already use instead.
    """
    rc_esc, escaped, err_esc = await run_command(["systemd-escape", "--path", disk_path])
    if rc_esc != 0:
        return rc_esc, "", f"systemd-escape failed: {err_esc}"
    unit = f"ahc-partition-format@{escaped.strip()}.service"
    return await run_command(["systemctl", "start", unit], timeout=600)


async def format_partition(partition_path: str, label: str) -> Tuple[int, str, str]:
    """mkfs.ext4 an already-existing partition with a caller-chosen *label*, in
    the HOST namespace. Same sandbox-escape reason as partition_and_format_device
    above — used by the manual /format endpoint (a different code path from
    smart-activate, which always uses the "AiHomeCloud" label and goes through
    partition_and_format_device instead)."""
    rc_esc, escaped, err_esc = await run_command(["systemd-escape", "--path", partition_path])
    if rc_esc != 0:
        return rc_esc, "", f"systemd-escape failed: {err_esc}"
    unit = f"ahc-format@{escaped.strip()}.service"
    return await run_command(
        ["systemctl", "start", f"--setenv=AHC_FORMAT_LABEL={label}", unit], timeout=600
    )

_DLNA_SERVICE_CANDIDATES: tuple[str, ...] = ("minidlna", "minidlnad")

# Short TTL cache for lsblk results — avoids repeated subprocess calls
# when the screen loads devices then immediately activates.
_lsblk_cache: List[dict] = []
_lsblk_cache_ts: float = 0.0
_LSBLK_CACHE_TTL: float = 3.0  # seconds

# The real root disk's kernel name (e.g. "nvme0n1", "sda"), resolved from
# whichever partition is mounted at "/" -- refreshed each time list_block_devices()
# fetches fresh lsblk data. See is_os_partition()'s doc comment for why this exists.
_root_disk_cache: Optional[str] = None

# ── Size helpers ─────────────────────────────────────────────────────────────

_SIZE_SUFFIXES = ["B", "KB", "MB", "GB", "TB"]


def _human_size(n: int) -> str:
    """Convert bytes to a human-readable string like '64.0 GB'."""
    val = float(n)
    for suffix in _SIZE_SUFFIXES:
        if val < 1024.0:
            return f"{val:.1f} {suffix}"
        val /= 1024.0
    return f"{val:.1f} PB"


# ── Device classification helpers ────────────────────────────────────────────

def classify_transport(device: dict) -> str:
    """Classify a block device into 'usb', 'nvme', or 'sd'."""
    tran = (device.get("tran") or "").lower()
    name = (device.get("name") or "").lower()

    if tran == "usb":
        return "usb"
    if tran == "nvme" or name.startswith("nvme"):
        return "nvme"
    if name.startswith("mmcblk"):
        return "sd"
    if tran:
        return tran
    return "unknown"


# Prefixes of device names that are always internal/OS storage.
# These must never be formatted regardless of mount state.
_OS_NAME_PREFIXES = (
    "mmcblk",   # SD card / eMMC (Cubie A7A OS disk)
    "mtdblock", # SPI/NAND flash (firmware / bootloader storage)
    "zram",     # Compressed RAM — never a real block device to format
    "loop",     # Loop devices — not real hardware
)

# Mount points that indicate a partition is part of the OS.
# Covers standard Linux, Raspberry Pi, and ARM SBC layout variants.
_OS_MOUNT_PREFIXES = (
    "/",
    "/boot",
    "/config",
    "/var",
    "/home",
    "/usr",
    "/opt",
    "/proc",
    "/sys",
)


def is_os_partition(device: dict) -> bool:
    """Return True if this partition must NOT be formatted.

    Blocks:
    - Internal non-hot-pluggable storage by device name prefix (mmcblk, mtd, zram, loop)
      -- ARM/eMMC-board naming; catches the OS disk by name alone regardless of mount state.
    - Any partition mounted at a system path (/, /boot, /boot/efi, /config, ...)
    - The real root disk (resolved from lsblk, see _root_disk_cache) and any of its
      partitions/siblings -- covers x86 (sda/nvme0n1 OS disks), where the name-prefix
      check above doesn't apply (those prefixes are legitimate EXTERNAL-drive names too),
      and the mountpoint check alone only protects *currently mounted* partitions, not an
      unmounted sibling (e.g. a swap or recovery partition) on the same physical OS disk.
      Found auditing x86 thin-client support, 2026-07-17.
    - OS-partition detection is size-agnostic — external USB/NVMe of any size is formattable
      provided it is not mounted at a system path.
    """
    name = (device.get("name") or "").lower()
    pkname = (device.get("pkname") or "").lower()
    mountpoint = (device.get("mountpoint") or "").rstrip("/")

    # Block all known-internal device types by name prefix
    if any(name.startswith(prefix) for prefix in _OS_NAME_PREFIXES):
        return True

    # Block if this partition is mounted at a system path (None/empty mountpoint = safe)
    if mountpoint and any(
        mountpoint == mp or mountpoint.startswith(mp + "/")
        for mp in _OS_MOUNT_PREFIXES
    ):
        return True

    # Block the real root disk itself (disk-level check) and any of its partitions
    # (partition-level check via pkname), even when unmounted or not name-prefix-matched.
    root_disk = (_root_disk_cache or "").lower()
    if root_disk and (name == root_disk or pkname == root_disk):
        return True

    return False


# ── lsblk helpers ────────────────────────────────────────────────────────────

async def list_block_devices(skip_cache: bool = False) -> List[dict]:
    """Run lsblk -J -b and return the raw JSON device list.

    Results are cached for _LSBLK_CACHE_TTL seconds to avoid redundant
    subprocess calls when the frontend loads devices then immediately
    activates.  Pass skip_cache=True after a mutation (mount/format).
    """
    global _lsblk_cache, _lsblk_cache_ts, _root_disk_cache  # noqa: PLW0603
    now = time.monotonic()
    if not skip_cache and _lsblk_cache and (now - _lsblk_cache_ts) < _LSBLK_CACHE_TTL:
        return _lsblk_cache
    try:
        rc, out, err = await run_command([
            "lsblk", "-J", "-b",
            "-o", "NAME,SIZE,TYPE,MOUNTPOINT,FSTYPE,LABEL,MODEL,TRAN,SERIAL,PKNAME",
        ])
        if rc != 0:
            logger.error("lsblk failed: %s", err)
            return []
        data = json.loads(out or "{}")
        devices = data.get("blockdevices", [])
        _lsblk_cache = devices
        _lsblk_cache_ts = now
        _root_disk_cache = _resolve_root_disk_name(devices)
        return devices
    except ValueError:
        logger.warning("lsblk validation failed")
        return []
    except Exception as e:
        logger.error("Failed to list block devices: %s", e)
        return []


def _resolve_root_disk_name(devices: list) -> Optional[str]:
    """Find the real root disk's kernel name (e.g. "nvme0n1", "sda", "mmcblk0") by
    locating whichever partition is mounted at exactly "/" and reading its parent
    disk name (PKNAME, requested in the lsblk query above). Falls back to the
    device's own name if it has no PKNAME (a disk with no partition table, mounted
    directly). Returns None if nothing is mounted at "/" in this lsblk snapshot --
    a defense-in-depth addition, so failing to resolve just skips this extra check
    rather than weakening is_os_partition()'s prior behavior.
    """
    for part in flatten_devices(devices):
        if part.get("mountpoint") == "/":
            pkname = part.get("pkname") or ""
            return (pkname or part.get("name") or "").lower() or None
    return None


def flatten_devices(devices: list, parent: Optional[dict] = None) -> list:
    """Flatten nested lsblk tree into a list of partitions."""
    result = []
    for dev in devices:
        dev_type = (dev.get("type") or "").lower()
        children = dev.get("children", [])

        if parent:
            if not dev.get("model"):
                dev["model"] = parent.get("model")
            if not dev.get("tran"):
                dev["tran"] = parent.get("tran")
            if not dev.get("serial"):
                dev["serial"] = parent.get("serial")

        if dev_type == "part":
            result.append(dev)
        elif dev_type == "disk" and not children:
            result.append(dev)

        if children:
            result.extend(flatten_devices(children, parent=dev))

    return result


async def find_partition(device_path: str) -> Optional[dict]:
    """Look up a specific device by path (e.g. '/dev/sda1')."""
    raw_devices = await list_block_devices()
    partitions = flatten_devices(raw_devices)
    return next(
        (p for p in partitions if f"/dev/{p['name']}" == device_path),
        None,
    )


def _partition_path(disk_name: str) -> str:
    """Derive the first partition device path from a disk name.

    sda     → /dev/sda1       (traditional SCSI/SATA disk)
    nvme0n1 → /dev/nvme0n1p1  (NVMe — name ends with a digit)
    mmcblk0 → /dev/mmcblk0p1  (eMMC — same rule)
    """
    if disk_name and disk_name[-1].isdigit():
        return f"/dev/{disk_name}p1"
    return f"/dev/{disk_name}1"


def disk_name_from_partition(part_name: str) -> str:
    """Derive the parent disk's kernel name from a partition name — the reverse of
    _partition_path(). A bare `rstrip("0123456789")` is wrong for the NVMe/eMMC "pN" naming
    scheme: "nvme0n1p1".rstrip(digits) stops at the non-digit "p", leaving "nvme0n1p" instead
    of "nvme0n1". That silently breaks anything comparing the result against the real disk name
    (e.g. an OS-disk safety check) for any board whose OS or data disk uses NVMe/eMMC naming —
    found live 2026-07-30 via Stage 2's storage council review, while fixing eject_device's
    missing is_os_partition() check (which depends on getting this name right).

    sda1       → sda        (traditional SCSI/SATA disk)
    nvme0n1p1  → nvme0n1    (NVMe)
    mmcblk0p1  → mmcblk0    (eMMC)
    """
    stripped = part_name.rstrip("0123456789")
    if stripped.endswith("p") and stripped[:-1] and stripped[:-1][-1].isdigit():
        return stripped[:-1]
    return stripped


def _display_name(disk: dict) -> str:
    """Return a human-friendly drive name. No /dev/ paths or technical terms."""
    model = (disk.get("model") or "").strip()
    tran = classify_transport(disk)
    size_bytes = int(disk.get("size") or 0)
    size_str = _human_size(size_bytes)
    transport_label = {"usb": "USB Drive", "nvme": "NVMe Drive"}.get(tran, "Drive")
    if model:
        return f"{model} ({size_str})"
    return f"{size_str} {transport_label}"


def _find_best_partition(children: list) -> Optional[dict]:
    """Find the best usable partition for mounting/activating.

    Priority:
    1. Largest ext4 partition that is not an OS partition
    2. Largest non-OS partition of any filesystem
    Returns None if all partitions are OS partitions or none exist.
    """
    usable = [p for p in children if not is_os_partition(p)]
    if not usable:
        return None
    # Prefer ext4 — ready to mount without formatting
    ext4_parts = [p for p in usable if (p.get("fstype") or "") == "ext4"]
    if ext4_parts:
        return max(ext4_parts, key=lambda p: int(p.get("size") or 0))
    # Fall back to largest usable partition of any type
    return max(usable, key=lambda p: int(p.get("size") or 0))


def build_device_list(raw_devices: list) -> list[StorageDevice]:
    """Convert raw lsblk disk-level devices to StorageDevice models.

    Returns ONE entry per physical disk (not per partition).
    OS disks and unsupported transports are filtered out.
    Call with the output of list_block_devices() directly — no need to
    flatten first.  flatten_devices() is still available for other uses
    (e.g. find_partition).
    """
    nas_root_str = str(settings.nas_root)
    result: list[StorageDevice] = []

    for disk in raw_devices:
        dev_type = (disk.get("type") or "").lower()
        if dev_type != "disk":
            continue

        # Filter by transport — only external hot-plug storage, EXCEPT the disk
        # currently mounted at nas_root: on VM deployments (e.g. the demo VPS)
        # the data volume shows up as plain SCSI/virtio, and hiding it would
        # leave the Storage page's drive list empty while the drive is active.
        transport = classify_transport(disk)
        if transport not in ("usb", "nvme"):
            active_children = disk.get("children") or [disk]
            is_active_nas_disk = any(
                (c.get("mountpoint") or "") == nas_root_str for c in active_children
            )
            if not is_active_nas_disk:
                continue

        # Skip OS disks (mmcblk, zram, loop, system-mount prefixes)
        if is_os_partition(disk):
            continue

        children = disk.get("children", [])
        best = _find_best_partition(children)
        best_partition_path: Optional[str] = (
            f"/dev/{best['name']}" if best else None
        )

        # Determine mount / NAS-active state across all partitions
        # (an unpartitioned disk can itself be mounted directly)
        candidates = children if children else [disk]
        active_candidate = next(
            (c for c in candidates if c.get("mountpoint") == nas_root_str),
            None,
        )
        any_mounted_candidate = next(
            (c for c in candidates if c.get("mountpoint")),
            None,
        )
        mount_point: Optional[str] = (
            (active_candidate or any_mounted_candidate or {}).get("mountpoint")
        )

        name = disk.get("name", "")
        size_bytes = int(disk.get("size") or 0)

        result.append(StorageDevice(
            name=name,
            path=f"/dev/{name}",
            sizeBytes=size_bytes,
            sizeDisplay=_human_size(size_bytes),
            fstype=best.get("fstype") if best else None,
            label=best.get("label") if best else None,
            model=(disk.get("model") or "").strip() or None,
            transport=transport,
            mounted=bool(any_mounted_candidate),
            mountPoint=mount_point,
            isNasActive=bool(active_candidate),
            isOsDisk=False,
            displayName=_display_name(disk),
            bestPartition=best_partition_path,
        ))

    return result


# ── NAS service helpers ──────────────────────────────────────────────────────

async def stop_nas_services():
    """Best-effort stop of NAS-related services before unmount."""
    dlna_svc = await resolve_dlna_service_name()
    services = ["smbd", "nmbd", "nfs-kernel-server"]
    if dlna_svc:
        services.append(dlna_svc)
    for svc in services:
        try:
            await run_command(["systemctl", "stop", svc])
        except Exception:
            pass


async def start_nas_services():
    """Best-effort start of NAS services after mount."""
    dlna_svc = await resolve_dlna_service_name()
    services = ["smbd", "nmbd"]
    if dlna_svc:
        services.append(dlna_svc)
    for svc in services:
        try:
            await run_command(["systemctl", "start", svc])
        except Exception:
            pass


async def _service_exists(service_name: str) -> bool:
    """Return True when a systemd unit exists for *service_name*."""
    rc, out, _ = await run_command(
        ["systemctl", "show", "-p", "LoadState", "--value", service_name],
        timeout=10,
    )
    if rc != 0:
        return False
    return out.strip() != "not-found"


async def resolve_dlna_service_name() -> Optional[str]:
    """Return the first available DLNA service unit name on this system."""
    for svc in _DLNA_SERVICE_CANDIDATES:
        if await _service_exists(svc):
            return svc
    return None


async def ensure_dlna_started_and_enabled() -> bool:
    """Enable + start the available DLNA service. Returns True when started."""
    dlna_svc = await resolve_dlna_service_name()
    if not dlna_svc:
        logger.info("DLNA service unit not found (tried: %s)", ", ".join(_DLNA_SERVICE_CANDIDATES))
        return False

    await run_command(["systemctl", "enable", dlna_svc], timeout=15)
    rc, _, err = await run_command(["systemctl", "start", dlna_svc], timeout=15)
    if rc != 0:
        logger.warning("Failed to start DLNA service %s: %s", dlna_svc, err)
        return False
    logger.info("DLNA service started and enabled: %s", dlna_svc)
    return True


# ── Open file handle check ───────────────────────────────────────────────────

async def check_open_handles() -> list[dict]:
    """
    Check for processes with open file handles on the NAS root.
    Returns a list of {pid, user, command, path} dicts.
    """
    nas_root = str(settings.nas_root)
    blockers: list[dict] = []

    # Try fuser first — fast mountpoint-level check (what umount uses internally).
    try:
        rc, _, stderr = await run_command(["fuser", "-v", "-m", nas_root], timeout=5)
        # fuser exits 0 when processes found, 1 when none; output goes to stderr.
        if stderr:
            lines = stderr.strip().split("\n")
            for line in lines[1:]:
                parts = line.split()
                if len(parts) >= 4:
                    blockers.append({
                        "command": parts[-1] if len(parts) > 4 else "unknown",
                        "pid": parts[1] if len(parts) > 1 else "?",
                        "user": parts[0] if parts else "?",
                        "path": nas_root,
                    })
        return blockers
    except FileNotFoundError:
        pass  # fuser not installed — fall through to lsof
    except Exception:
        pass

    # Fallback: lsof +D — slower (recursive directory walk) but more detailed.
    try:
        rc, out, _ = await run_command(["lsof", "+D", nas_root], timeout=30)
        if rc == 0 and out:
            lines = out.strip().split("\n")
            for line in lines[1:]:
                parts = line.split()
                if len(parts) >= 9:
                    blockers.append({
                        "command": parts[0],
                        "pid": parts[1],
                        "user": parts[2],
                        "path": parts[8] if len(parts) > 8 else nas_root,
                    })
    except Exception:
        logger.warning("Neither fuser nor lsof available for handle check")

    return blockers


# ── Core unmount logic (shared by unmount + eject) ───────────────────────────

async def do_unmount(force: bool = False) -> str:
    """
    Perform the full unmount sequence: stop services -> sync -> umount.
    Returns the device path that was unmounted.
    Raises HTTPException on failure.
    """
    storage_state = await store.get_storage_state()

    if not storage_state.get("activeDevice"):
        raise HTTPException(400, "No device is currently mounted")

    device_path = storage_state["activeDevice"]

    if not force:
        blockers = await check_open_handles()
        nas_service_cmds = {"smbd", "nmbd", "nfsd", "rpc.mountd", "minidlnad"}
        user_blockers = [
            b for b in blockers
            if b["command"] not in nas_service_cmds
        ]
        if user_blockers:
            raise HTTPException(
                409,
                detail={
                    "error": "files_in_use",
                    "message": f"{len(user_blockers)} process(es) have open files on the NAS",
                    "blockers": user_blockers,
                },
            )

    await stop_nas_services()

    try:
        await run_command(["sync"])
    except Exception:
        pass

    rc, _, stderr = await unmount_nas_device()
    if rc != 0:
        raise HTTPException(
            500,
            f"Unmount failed (files may be in use): {stderr}",
        )

    await store.clear_storage_state()

    logger.info("Unmounted %s from %s", device_path, settings.nas_root)
    return device_path
