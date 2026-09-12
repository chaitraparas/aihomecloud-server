"""
Storage routes — endpoint handlers for device listing, scan, format, mount,
unmount, eject, stats, and auto-remount.

Helper functions (lsblk parsing, device classification, mount/unmount logic)
are in storage_helpers.py.
"""

import asyncio
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from shutil import disk_usage
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from starlette.requests import Request

from ..limiter import limiter

from ..auth import get_current_user, require_admin
from .. import device_locks
from ..config import settings
from ..models import (
    CheckUsageResponse,
    DeviceActionResponse,
    EjectRequest,
    JobStartedResponse,
    MountResponse,
    SmartActivateResponse,
    FormatRequest,
    MountRequest,
    SmartActivateRequest,
    StorageDevice,
    StorageStats,
)
from ..job_store import JobStatus, create_job, update_job
from .. import store
from ..audit import audit_log
from ..subprocess_runner import run_command
from .event_routes import emit_device_mounted, emit_device_ejected
from .storage_helpers import (
    _display_name,
    _find_best_partition,
    _partition_path,
    build_device_list,
    check_open_handles,
    classify_transport,
    disk_name_from_partition,
    do_unmount,
    find_partition,
    is_os_partition,
    list_block_devices,
    mount_nas_device,
    partition_and_format_device,
    format_partition,
    start_nas_services,
    unmount_nas_device,
)

logger = logging.getLogger("aihomecloud.storage")

router = APIRouter(prefix="/api/v1/storage", tags=["storage"])


@router.get("/devices", response_model=List[StorageDevice])
async def list_devices(user: dict = Depends(get_current_user)):
    """List all physical drives on the system (one entry per disk)."""
    raw_devices = await list_block_devices()
    return build_device_list(raw_devices)


# ── 2A.2  Scan ───────────────────────────────────────────────────────────────

@router.get("/scan", response_model=List[StorageDevice])
@limiter.limit("10/minute")
async def scan_devices(request: Request, user: dict = Depends(get_current_user)):
    """
    Re-scan for newly connected block devices.
    Triggers udev to re-detect, waits for settle, then returns fresh list.
    """
    try:
        await run_command(["sudo", "-n", "udevadm", "trigger", "--subsystem-match=block"])
        await run_command(["sudo", "-n", "udevadm", "settle", "--timeout=3"])
    except Exception as e:
        logger.warning("udevadm not available: %s", e)

    # Return fresh device list (reuse existing logic)
    raw_devices = await list_block_devices(skip_cache=True)
    return build_device_list(raw_devices)


# ── Smart-activate helpers ───────────────────────────────────────────────────

# Positive identity marker written to the NAS root once a drive has been
# explicitly activated. Blind auto-mount requires this marker before adopting
# an unknown drive, so an unrelated ext4 USB stick is never silently taken over.
_NAS_MARKER_NAME = ".ahc_nas_marker"
_NAS_MARKER_CONTENT = "aihomecloud-nas-v1"


def _write_nas_marker() -> None:
    """Stamp the NAS root with the AHC identity marker (best-effort)."""
    try:
        (settings.nas_root / _NAS_MARKER_NAME).write_text(_NAS_MARKER_CONTENT)
    except Exception:
        logger.warning("Could not write NAS marker at %s", settings.nas_root, exc_info=True)


def _has_nas_marker() -> bool:
    """Return True if the currently-mounted NAS root carries the AHC marker."""
    return (settings.nas_root / _NAS_MARKER_NAME).exists()


async def _post_mount_setup(partition_path: str, disk: dict, display_name: str) -> None:
    """After a successful mount: create dirs, persist storage state, start services."""
    nas_root = str(settings.nas_root)
    settings.personal_path.mkdir(parents=True, exist_ok=True)
    settings.family_path.mkdir(parents=True, exist_ok=True)
    settings.entertainment_path.mkdir(parents=True, exist_ok=True)
    _write_nas_marker()
    await store.save_storage_state({
        "activeDevice": partition_path,
        "mountedAt": nas_root,
        "displayName": display_name,
        "transport": classify_transport(disk),
        "model": (disk.get("model") or "").strip(),
        "mountedSince": datetime.now(timezone.utc).isoformat(),
    })
    await start_nas_services()


async def _smart_format_and_mount(job_id: str, disk_name: str, display_name: str) -> None:
    """Async job: wipe disk → GPT partition → mkfs.ext4 → mount → post-setup.

    Uses run_command() — never shell=True.
    Uses settings.nas_root — never a hardcoded path.
    """
    disk_path = f"/dev/{disk_name}"
    partition_path = _partition_path(disk_name)
    try:
        nas_root = str(settings.nas_root)

        # Wipe + GPT-partition + mkfs.ext4, all in one host-namespace oneshot unit — see
        # partition_and_format_device()'s docstring for why this can't be a direct subprocess
        # call (same ProtectSystem=strict + NoNewPrivileges=yes sandbox escape as mount/umount).
        logger.warning("SMART-ACTIVATE: partitioning + formatting %s as ext4", disk_path)
        rc, _, stderr = await partition_and_format_device(disk_path)
        if rc != 0:
            update_job(job_id, status=JobStatus.failed, error=f"Partitioning/format failed: {stderr}")
            return

        # Step 4: Mount
        settings.nas_root.mkdir(parents=True, exist_ok=True)
        rc, _, stderr = await mount_nas_device(partition_path)
        if rc != 0:
            update_job(job_id, status=JobStatus.failed, error=f"Mount failed: {stderr}")
            return

        # Step 5: Post-mount setup (dirs, state, services, event)
        disk_stub = {"name": disk_name, "tran": "", "model": ""}
        await _post_mount_setup(partition_path, disk_stub, display_name)
        await emit_device_mounted(partition_path, nas_root)

        update_job(
            job_id,
            status=JobStatus.completed,
            result={"action": "formatted_and_mounted", "device": partition_path},
        )
        logger.info("SMART-ACTIVATE: %s ready at %s", partition_path, nas_root)
    except Exception as e:
        update_job(job_id, status=JobStatus.failed, error=str(e))
        logger.exception("SMART-ACTIVATE job failed: %s", e)


# ── Smart-activate endpoint ──────────────────────────────────────────────────

@router.post("/smart-activate", response_model=SmartActivateResponse, response_model_exclude_none=True)
@limiter.limit("5/minute")
async def smart_activate(
    request: Request,
    req: SmartActivateRequest,
    user: dict = Depends(require_admin),
):
    """Drive setup, given the caller's explicit format choice (req.format).

    Accepts a whole-disk path (e.g. /dev/sda):
      format=False → mount whatever filesystem is already on the disk's best
                      partition as-is (any type mount(8) auto-detects — ext4,
                      ntfs, exfat, ...), never touching existing data. 400 if
                      the disk has no existing filesystem to use.
      format=True  → always wipe and create a fresh ext4 filesystem, start
                      async job, return action=formatting + jobId.
      Already active → return action=already_active (idempotent either way).

    Response always includes display_name (no /dev/ paths or tech jargon).
    """
    # ── Already active: idempotent regardless of format choice ────────────────
    storage_state = await store.get_storage_state()
    active_device = storage_state.get("activeDevice", "")
    # Comparing a stored PARTITION path against a caller-supplied DISK path, so exact
    # equality is not possible — but a bare startswith is looser than the relationship needs:
    # req.device="/dev/sd" would match active_device="/dev/sda1". Normalising both to their
    # parent disk expresses the intended "same physical device" test exactly, and rejects the
    # prefix that only happens to share characters. (2026-07-30 storage finding 7.)
    if active_device and device_locks.base_disk(active_device) == device_locks.base_disk(req.device):
        # Re-read lsblk for a fresh display_name if possible
        raw = await list_block_devices()
        disk = next((d for d in raw if f"/dev/{d['name']}" == req.device), None)
        display = (
            _display_name(disk) if disk
            else storage_state.get("displayName", req.device)
        )
        return {"action": "already_active", "display_name": display}

    # ── Find disk in lsblk ───────────────────────────────────────────────────
    raw = await list_block_devices()
    disk = next((d for d in raw if f"/dev/{d['name']}" == req.device), None)
    if disk is None:
        raise HTTPException(404, f"Drive not found: {req.device}")

    # ── Security: never touch OS storage ─────────────────────────────────────
    if is_os_partition(disk):
        raise HTTPException(403, "Cannot use system storage as a data drive")

    display = _display_name(disk)
    disk_name = disk["name"]
    children = disk.get("children", [])
    best = _find_best_partition(children)

    # ── Explicit "use as-is": mount whatever's already there, never format ────
    if not req.format:
        if not best or not (best.get("fstype") or ""):
            raise HTTPException(
                400,
                "This drive has no existing filesystem to use as-is — it needs to be formatted first.",
            )
        partition_dev = f"/dev/{best['name']}"
        nas_root = str(settings.nas_root)

        # Already mounted at our target — storage state may be stale after a
        # service restart. Re-sync state and return success without re-mounting.
        current_mountpoint = (best.get("mountpoint") or "").rstrip("/")
        if current_mountpoint == nas_root.rstrip("/"):
            logger.info("smart-activate: %s already mounted at %s — syncing state", partition_dev, nas_root)
            await _post_mount_setup(partition_dev, disk, display)
            await emit_device_mounted(partition_dev, nas_root)
            return {"action": "mounted", "display_name": display}

        settings.nas_root.mkdir(parents=True, exist_ok=True)
        rc, _, stderr = await mount_nas_device(partition_dev)
        if rc != 0:
            raise HTTPException(500, f"Could not activate drive: {stderr}")
        await _post_mount_setup(partition_dev, disk, display)
        await emit_device_mounted(partition_dev, nas_root)
        return {"action": "mounted", "display_name": display}

    # ── Explicit "format": always wipe + fresh ext4, start async job ──────────
    # Same claim-before-dispatch as /format: this path also runs sgdisk + mkfs, so two of
    # these — or one of these racing a /format — must not both proceed on one disk.
    disk_path_for_lock = f"/dev/{disk_name}"
    try:
        await device_locks.acquire(disk_path_for_lock, f"smart-activate:{user.get('sub', '')}")
    except device_locks.DeviceBusyError as busy:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Another storage operation is already running on {busy.device} ({busy.holder}).",
        )

    job = create_job(user_id=user.get("sub", ""))
    update_job(job.id, status=JobStatus.running)

    async def _locked_smart_format() -> None:
        try:
            await _smart_format_and_mount(job.id, disk_name, display)
        finally:
            await device_locks.release(disk_path_for_lock)

    asyncio.create_task(_locked_smart_format())
    return {"action": "formatting", "display_name": display, "jobId": job.id}


# ── 2B.2  Pre-unmount check ──────────────────────────────────────────────────

@router.get("/check-usage", response_model=CheckUsageResponse, response_model_exclude_none=True)
# Read-only, but it runs fuser and `lsof +D` — a real subprocess plus a filesystem walk.
# Its mutating siblings are all limited; this one was missed. (2026-07-30 finding 8.)
@limiter.limit("30/minute")
async def check_usage(request: Request, user: dict = Depends(get_current_user)):
    """
    Check for processes with open file handles on the NAS mount.
    Call before unmount to show the user what's blocking.
    Returns {blockers: [...], safe: bool}.
    """
    storage_state = await store.get_storage_state()
    if not storage_state.get("activeDevice"):
        return {"blockers": [], "safe": True, "message": "No device mounted"}

    blockers = await check_open_handles()

    # Filter out NAS service processes (will be stopped during unmount)
    nas_service_cmds = {"smbd", "nmbd", "nfsd", "rpc.mountd", "minidlnad"}
    user_blockers = [
        b for b in blockers if b["command"] not in nas_service_cmds
    ]
    service_blockers = [
        b for b in blockers if b["command"] in nas_service_cmds
    ]

    return {
        "blockers": user_blockers,
        "serviceBlockers": service_blockers,
        "safe": len(user_blockers) == 0,
        "message": (
            "Safe to unmount"
            if len(user_blockers) == 0
            else f"{len(user_blockers)} process(es) have open files"
        ),
    }


# ── 2A.3  Format ─────────────────────────────────────────────────────────────

@router.post("/format", response_model=JobStartedResponse)
@limiter.limit("5/minute")
async def format_device(
    request: Request,
    req: FormatRequest,
    user: dict = Depends(require_admin),
):
    """
    Format a device as ext4.
    Safety: confirmDevice must match device path (like GitHub repo-delete).
    """
    if req.confirm_device != req.device:
        raise HTTPException(
            400,
            "Device confirmation does not match — please confirm the device path.",
        )

    # Validate label: ext4 requires ≤16 chars, alphanumeric/hyphens/underscores only.
    if not re.match(r'^[a-zA-Z0-9_-]{1,16}$', req.label):
        raise HTTPException(
            400,
            "Label must be 1–16 characters and contain only letters, digits, hyphens, or underscores.",
        )

    target = await find_partition(req.device)

    if not target:
        raise HTTPException(404, f"Device {req.device} not found")
    if is_os_partition(target):
        raise HTTPException(403, "Cannot format an OS partition")
    if target.get("mountpoint"):
        raise HTTPException(409, "Device is currently mounted — unmount first")

    # Claim the disk BEFORE the job is created, so a second request racing this one is
    # refused rather than queued behind it. Two concurrent mkfs runs on one device was
    # storage finding 5 of the 2026-07-30 bug hunt.
    try:
        await device_locks.acquire(req.device, f"format:{user.get('sub', '')}")
    except device_locks.DeviceBusyError as busy:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Another storage operation is already running on {busy.device} ({busy.holder}).",
        )

    job = create_job(user_id=user.get("sub", ""))
    update_job(job.id, status=JobStatus.running)

    async def _run_format_job() -> None:
        try:
            logger.warning("FORMATTING %s as ext4 (label=%s)", req.device, req.label)

            rc, _, stderr = await format_partition(req.device, req.label)

            if rc != 0:
                update_job(job.id, status=JobStatus.failed, error=f"Format failed: {stderr}")
                return

            update_job(
                job.id,
                status=JobStatus.completed,
                result={
                    "status": "formatted",
                    "device": req.device,
                    "fstype": "ext4",
                    "label": req.label,
                },
            )
            audit_log("storage_formatted", actor_id=user.get("sub", ""), device=req.device, label=req.label)
            logger.info("Formatted %s successfully", req.device)
        except Exception as e:
            update_job(job.id, status=JobStatus.failed, error=str(e))

    async def _run_with_timeout() -> None:
        try:
            await asyncio.wait_for(_run_format_job(), timeout=600)
        except asyncio.TimeoutError:
            update_job(job.id, status=JobStatus.failed, error="Format job timed out after 10 minutes")
        finally:
            # Released here, not in _run_format_job, so a timeout or cancellation also frees
            # the disk. Otherwise one stuck format makes the device unformattable until the
            # service restarts.
            await device_locks.release(req.device)

    asyncio.create_task(_run_with_timeout())
    return {"jobId": job.id}


# ── 2A.4  Mount ──────────────────────────────────────────────────────────────

@router.post("/mount", response_model=MountResponse)
@limiter.limit("5/minute")
async def mount_device(
    request: Request,
    req: MountRequest,
    user: dict = Depends(require_admin),
):
    """Mount a device at the NAS root (/srv/nas)."""
    nas_root = str(settings.nas_root)

    # Check if something is already active
    storage_state = await store.get_storage_state()
    if storage_state.get("activeDevice"):
        raise HTTPException(
            409,
            f"Device {storage_state['activeDevice']} is already mounted at {nas_root}",
        )

    target = await find_partition(req.device)

    if not target:
        raise HTTPException(404, f"Device {req.device} not found")
    if is_os_partition(target):
        raise HTTPException(403, "Cannot mount an OS partition as NAS storage")
    if not target.get("fstype"):
        raise HTTPException(400, "Device has no filesystem — format it first")

    # Ensure mount point exists
    settings.nas_root.mkdir(parents=True, exist_ok=True)

    # Mount
    rc, _, stderr = await mount_nas_device(req.device)
    if rc != 0:
        raise HTTPException(500, f"Mount failed: {stderr}")

    # Create standard NAS directories
    settings.personal_path.mkdir(parents=True, exist_ok=True)
    settings.family_path.mkdir(parents=True, exist_ok=True)
    settings.entertainment_path.mkdir(parents=True, exist_ok=True)

    # Persist mount state
    await store.save_storage_state({
        "activeDevice": req.device,
        "mountedAt": nas_root,
        "fstype": target.get("fstype", ""),
        "label": target.get("label", ""),
        "model": (target.get("model") or "").strip(),
        "transport": classify_transport(target),
        "mountedSince": datetime.now(timezone.utc).isoformat(),
    })

    # Start NAS services
    await start_nas_services()

    logger.info("Mounted %s at %s", req.device, nas_root)
    await emit_device_mounted(req.device, nas_root)
    return {"status": "mounted", "device": req.device, "mountPoint": nas_root}


# ── 2A.5  Unmount ────────────────────────────────────────────────────────────

@router.post("/unmount", response_model=DeviceActionResponse)
@limiter.limit("5/minute")
async def unmount_device(
    request: Request,
    force: bool = False,
    user: dict = Depends(require_admin),
):
    """
    Safely unmount the NAS storage (stop services → sync → umount).
    Set force=true to skip the open-file-handle check.
    """
    device_path = await do_unmount(force=force)
    return {"status": "unmounted", "device": device_path}


# ── 2A.6  Eject ──────────────────────────────────────────────────────────────

@router.post("/eject", response_model=DeviceActionResponse)
@limiter.limit("5/minute")
async def eject_device(
    request: Request,
    req: EjectRequest,
    user: dict = Depends(require_admin),
):
    """
    Safely eject a USB device: unmount → power off USB port.
    Only works for USB-connected storage.
    """
    # Extract parent disk name  (e.g. "/dev/sda1" → "sda", "/dev/nvme0n1p1" → "nvme0n1")
    dev_name = req.device.split("/")[-1]          # "sda1" / "nvme0n1p1"
    disk_name = disk_name_from_partition(dev_name)  # "sda" / "nvme0n1"

    # Security: never touch OS storage. Every other storage-mutating endpoint
    # (format/mount/smart-activate) already refuses to operate on the OS disk — eject was
    # missing this same check, so an admin passing the OS boot disk's path would skip straight
    # to writing sysfs 'delete' (or udisksctl power-off) on the device the system is currently
    # running from. Checked against disk_name directly (not via find_partition(req.device),
    # which only returns leaf partitions/childless disks — a disk WITH partitions, like a
    # typical OS disk, wouldn't be found that way and the check would silently no-op). Found
    # live 2026-07-30 via Stage 2's storage council review.
    await list_block_devices()  # ensure the root-disk cache is warm
    if is_os_partition({"name": disk_name}):
        raise HTTPException(403, "Cannot eject system storage")

    storage_state = await store.get_storage_state()
    active_device = storage_state.get("activeDevice", "")

    # If the device to eject is currently mounted, unmount first (force=True)
    if active_device == req.device:
        await do_unmount(force=True)

    # Power off USB device via sysfs
    delete_path = Path(f"/sys/block/{disk_name}/device/delete")
    try:
        if delete_path.exists():
            try:
                delete_path.write_text("1")
                logger.info("Ejected USB device %s via sysfs", disk_name)
            except Exception:
                # Try udisksctl as fallback
                rc, _, stderr = await run_command(["udisksctl", "power-off", "-b", f"/dev/{disk_name}"])
                if rc == 0:
                    logger.info("Ejected USB device %s via udisksctl", disk_name)
                else:
                    logger.warning(
                        "Could not power off %s — device may need manual removal: %s",
                        disk_name, stderr,
                    )
        else:
            # Try udisksctl as fallback
            rc, _, stderr = await run_command(["udisksctl", "power-off", "-b", f"/dev/{disk_name}"])
            if rc == 0:
                logger.info("Ejected USB device %s via udisksctl", disk_name)
            else:
                logger.warning(
                    "Could not power off %s — device may need manual removal: %s",
                    disk_name, stderr,
                )
    except Exception as e:
        logger.warning("Could not power off USB device: %s", e)

    await emit_device_ejected(req.device)
    return {"status": "ejected", "device": req.device}


# ── 2A.7  Stats ──────────────────────────────────────────────────────────────

@router.get("/stats", response_model=StorageStats)
async def storage_stats(user: dict = Depends(get_current_user)):
    """
    Return disk usage for the NAS root.
    When an external device is mounted at /srv/nas, this automatically
    reports that device's capacity (shutil.disk_usage follows mount points).
    """
    try:
        usage = disk_usage(str(settings.nas_root))
        total_gb = round(usage.total / (1024 ** 3), 1)
        used_gb = round(usage.used / (1024 ** 3), 1)
    except Exception:
        total_gb = settings.total_storage_gb
        used_gb = 0.0

    return StorageStats(totalGB=total_gb, usedGB=used_gb)


# ── 2A.10  Auto-remount (called from main.py lifespan) ───────────────────────

# Labels that identify a drive as an AiHomeCloud NAS drive.
_AHC_LABELS = frozenset({"AiHomeCloud", "AiHomeNAS", "ahc_nas", "aihomecloud"})


def _is_nas_candidate(partition: dict) -> bool:
    """Return True if this partition looks like an AiHomeCloud NAS drive."""
    if (partition.get("fstype") or "") != "ext4":
        return False
    label = (partition.get("label") or "").strip()
    if label in _AHC_LABELS:
        return True
    # Also accept any unlabelled ext4 on a non-OS disk (generous fallback)
    return True


async def _scan_for_unmounted_nas_drive() -> str | None:
    """Scan all non-OS block devices for an unmounted ext4 partition.

    Returns the first partition path that looks like an AiHomeCloud NAS
    drive and is not currently mounted anywhere, or None if nothing found.
    Prefers drives whose label matches a known AHC label.
    """
    nas_root = str(settings.nas_root)
    try:
        raw = await list_block_devices(skip_cache=True)
    except Exception:
        return None

    # Build set of currently-mounted device paths from /proc/mounts
    mounted: set[str] = set()
    try:
        with open("/proc/mounts") as fh:
            for line in fh:
                parts = line.split()
                if parts:
                    mounted.add(parts[0])
    except Exception:
        pass

    candidates: list[tuple[int, str]] = []  # (priority, dev_path)
    for disk in raw:
        if is_os_partition(disk):
            continue
        transport = classify_transport(disk)
        if transport not in ("usb", "nvme"):
            continue
        for part in disk.get("children", []):
            if (part.get("fstype") or "") != "ext4":
                continue
            dev_path = f"/dev/{part['name']}"
            # Skip if already mounted somewhere
            if dev_path in mounted:
                # If it's already at our NAS root, tell caller by returning it
                mp = (part.get("mountpoint") or "").rstrip("/")
                if mp == nas_root.rstrip("/"):
                    return dev_path
                continue
            label = (part.get("label") or "").strip()
            priority = 0 if label in _AHC_LABELS else 1
            candidates.append((priority, dev_path))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


async def _do_mount_device(
    device_path: str, state_hint: dict | None = None, start_services: bool = True,
) -> bool:
    """Mount *device_path* at nas_root, create dirs, save state, start services.

    *start_services* lets the blind-scan auto-remount path (an UNKNOWN drive, trust not yet
    established) defer starting Samba/NFS/DLNA until after its own NAS-identity-marker check —
    without this, the mount+services-start sequence used to run unconditionally, so any random
    ext4 USB stick briefly had its contents served over the network for the several seconds it
    takes to mount, check the marker, and unmount again on a mismatch (a real TOCTOU window, not
    just an unmount delay: smbd can create lock files, NFS can export the path, minidlnad can
    start indexing, all before the marker check ever runs). Found live 2026-07-30 via Stage 2's
    storage council review. The known-device fast path (saved, previously-activated device) still
    defaults to True — that device was already trusted when it was first activated.

    Returns True on success, False on failure.
    """
    nas_root = str(settings.nas_root)
    settings.nas_root.mkdir(parents=True, exist_ok=True)

    for attempt in range(2):
        if attempt > 0:
            logger.info("Mount: retrying after 3s (attempt %d)…", attempt + 1)
            await asyncio.sleep(3)

        rc, _, stderr = await mount_nas_device(device_path)
        if rc == 0:
            logger.info("Mount: successfully mounted %s at %s", device_path, nas_root)
            settings.personal_path.mkdir(parents=True, exist_ok=True)
            settings.family_path.mkdir(parents=True, exist_ok=True)
            settings.entertainment_path.mkdir(parents=True, exist_ok=True)
            new_state = {
                **(state_hint or {}),
                "activeDevice": device_path,
                "mountedAt": nas_root,
                "mountedSince": datetime.now(timezone.utc).isoformat(),
            }
            await store.save_storage_state(new_state)
            if start_services:
                await start_nas_services()
            return True
        logger.warning("Mount attempt %d failed for %s: %s", attempt + 1, device_path, stderr)

    return False


async def try_auto_remount():
    """
    On startup, check storage.json for a previously-mounted device.
    If the device is still present and not yet mounted, remount it.

    If storage.json is empty (e.g. after a power cut cleared it), scan all
    non-OS USB/NVMe drives for an unmounted ext4 partition and mount the
    best candidate automatically.
    """
    state = await store.get_storage_state()
    device_path = state.get("activeDevice")
    nas_root = str(settings.nas_root)

    # ── Fast-path: check if something is already mounted at nas_root ──────────
    # A mountpoint-only /proc/mounts match isn't sufficient: this service's
    # ProtectSystem=strict + ReadWritePaths=<nas_root> makes systemd bind-mount nas_root onto
    # itself so it's writable inside the service's own private mount namespace, and that bind
    # mount is itself a real /proc/mounts entry for nas_root -- present regardless of whether an
    # actual external drive is mounted there. Confirmed the same defect exists in
    # local_backup.try_auto_remount() live 2026-07-15 (there it silently no-op'd every startup);
    # here it's worse -- blindly trusting parts[0] would corrupt storage.json's activeDevice with
    # the *root filesystem's own* device path. Guarded by comparing st_dev: a genuine external
    # mount has a different device ID than its own parent directory; a bind-mount-onto-itself
    # does not, since it's the same underlying filesystem just remounted.
    try:
        nas_root_path = Path(nas_root)
        is_real_mount = (
            nas_root_path.exists()
            and nas_root_path.stat().st_dev != nas_root_path.parent.stat().st_dev
        )
        if is_real_mount:
            with open("/proc/mounts") as fh:
                for line in fh:
                    parts = line.split()
                    if len(parts) >= 2 and parts[1].rstrip("/") == nas_root.rstrip("/"):
                        actual_dev = parts[0]
                        logger.info(
                            "Auto-remount: %s already mounted at %s — syncing state",
                            actual_dev, nas_root,
                        )
                        if not state.get("mountedAt") or state.get("activeDevice") != actual_dev:
                            state["activeDevice"] = actual_dev
                            state["mountedAt"] = nas_root
                            await store.save_storage_state(state)
                        return
    except Exception:
        pass

    # ── Known device path from storage.json ──────────────────────────────────
    if device_path:
        logger.info("Auto-remount: checking saved device %s", device_path)
        if not Path(device_path).exists():
            logger.warning("Auto-remount: saved device %s not found", device_path)
            await store.clear_storage_state()
            # Fall through to blind scan below
        else:
            if await _do_mount_device(device_path, state):
                # Saved device is trusted (previously activated) — ensure the
                # identity marker is present for legacy drives so future blind
                # scans recognise it.
                _write_nas_marker()
                return
            logger.error("Auto-remount: all attempts failed for saved device %s", device_path)
            await store.clear_storage_state()
            # Fall through to blind scan

    # ── Blind scan: storage.json empty or saved device failed ─────────────────
    logger.info("Auto-remount: scanning for unmounted NAS drive…")
    candidate = await _scan_for_unmounted_nas_drive()
    if not candidate:
        logger.info("Auto-remount: no unmounted NAS drive found")
        return

    logger.info("Auto-remount: found candidate %s — attempting mount", candidate)
    # Blind-scan adopts an UNKNOWN drive — defer starting Samba/NFS/DLNA until the identity
    # marker check below passes, so an unrelated ext4 USB stick is never briefly served over
    # the network before being rejected (see _do_mount_device's start_services docstring).
    if await _do_mount_device(candidate, start_services=False):
        if not _has_nas_marker():
            logger.warning(
                "Auto-remount: %s mounted but has no AHC NAS marker — "
                "unmounting and skipping (not an AiHomeCloud drive)", candidate,
            )
            await unmount_nas_device()
            await store.clear_storage_state()
            return
        await start_nas_services()
        logger.info("Auto-remount: blind-scan mount of %s succeeded", candidate)
    else:
        logger.error("Auto-remount: blind-scan mount of %s failed", candidate)


# ── Recovery endpoint (app + Telegram trigger) ───────────────────────────────

@router.post("/recover", response_model=DeviceActionResponse)
@limiter.limit("5/minute")
async def recover_storage(
    request: Request,
    user: dict = Depends(require_admin),
):
    """Scan for an unmounted NAS drive and mount it.

    Called by the app's 'Reconnect Storage' button and the Telegram /mount
    command when the drive is present but storage.json was lost (e.g. after a
    power cut).  Returns the mounted device path or raises 404.
    """
    nas_root = str(settings.nas_root)

    # Already mounted? Return immediately. A mountpoint-only /proc/mounts match isn't
    # sufficient -- this service's ProtectSystem=strict + ReadWritePaths=<nas_root> makes
    # systemd bind-mount nas_root onto itself so it's writable inside the service's own
    # private mount namespace, and that bind mount is itself a real /proc/mounts entry for
    # nas_root regardless of whether an actual external drive is mounted there. Found live
    # 2026-07-16 (full-repo audit): this exact endpoint -- the app's "Reconnect Storage"
    # button and the Telegram /mount command, both specifically meant for "drive present but
    # storage.json lost after a power cut" -- had the naive version, meaning it would report
    # false "already_mounted" success (with the root filesystem's own device path) even when
    # no external drive is mounted at all, silently defeating the very recovery flow it
    # exists for. Same guard as the already-fixed auto-remount fast-path above: compare
    # st_dev, not just the mountpoint string.
    try:
        nas_root_path = Path(nas_root)
        is_real_mount = (
            nas_root_path.exists()
            and nas_root_path.stat().st_dev != nas_root_path.parent.stat().st_dev
        )
        if is_real_mount:
            with open("/proc/mounts") as fh:
                for line in fh:
                    parts = line.split()
                    if len(parts) >= 2 and parts[1].rstrip("/") == nas_root.rstrip("/"):
                        return {"status": "already_mounted", "device": parts[0]}
    except Exception:
        pass

    candidate = await _scan_for_unmounted_nas_drive()
    if not candidate:
        raise HTTPException(404, "No unmounted NAS drive found")

    ok = await _do_mount_device(candidate)
    if not ok:
        raise HTTPException(500, f"Mount failed for {candidate}")

    await emit_device_mounted(candidate, nas_root)
    return {"status": "mounted", "device": candidate}
