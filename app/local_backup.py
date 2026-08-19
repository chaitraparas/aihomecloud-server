"""
Local Backup — a two-mode engine for NAS → secondary-drive (USB stick / SD card) backup.

Not the same feature as backup_routes.py (phone → NAS auto-backup).

Mode 1, "protected folders" (original): selective, snapshot+retention backup for the
small set of folders where point-in-time recovery from a bad overwrite matters (docs,
personal files). Each run is an `rsync --link-dest=<previous>` snapshot — unchanged files
are hardlinked (near-zero extra space), changed/new files are real copies, retention capped
at 3 snapshots so a small secondary drive doesn't fill up silently.

Mode 2, "media library" (2026-07-16, full-repo audit finding #1 — the media library itself
had exactly one copy): additive-mirror backup of the whole family/personal/entertainment
media tree. Deliberately NOT a snapshot: no --delete, no --link-dest, no retention pruning.
ingest() is append-only by construction (hash-dedup + _unique_dest() for name collisions,
soft-delete via trash — see ingest.py) so the "bad overwrite" threat that justifies
snapshot+retention for docs essentially can't happen to media; a snapshot engine over a
200GB+ photo tree would cost real inodes/rsync-stat-work/pruning budget a 1GB board can't
spare, for a threat that doesn't apply. A plain additive mirror (rsync -a, no --checksum —
size+mtime quick-check keeps this cheap) is the right shape and the cheapest one. Restore is
`rsync -a --ignore-existing` FROM the backup TO primary — purely additive on the primary
side, so it can never overwrite or clobber a file already there; the same command safely
covers both "recover a few lost files" and "fresh blank drive after disk death."

Design constraints driving the shape of this module:
  - Flash write-endurance: scheduled (nightly), not continuous/real-time sync —
    see main.py's _run_nightly_local_backup_sync.
  - Never two rsyncs at once on a low-power SBC: both modes share _sync_lock.
  - Media mode is opt-in (default off) — a large capacity commitment a family should
    choose deliberately, not something that starts mirroring the instant a USB stick with
    less free space than the library is plugged in.
"""

import asyncio
import logging
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

from .config import settings
from . import store
from .subprocess_runner import run_command

logger = logging.getLogger("aihomecloud.local_backup")

PROTECTED_FOLDERS_KEY = "local_backup_protected_folders"
DRIVE_STATE_KEY = "local_backup_drive_state"
MEDIA_ENABLED_KEY = "local_backup_media_enabled"
MEDIA_STATE_KEY = "local_backup_media_state"
_SNAPSHOT_RETENTION = 3
_RSYNC_TIMEOUT = 3600  # 1h ceiling per folder — generous for a family-scale docs folder
_MEDIA_RSYNC_TIMEOUT = 21600  # 6h ceiling — a whole media library, not a small docs folder
_MEDIA_MIN_FREE_BYTES = 500 * 1024 * 1024  # abort rather than silently fill the drive

_sync_lock = asyncio.Lock()


def is_syncing() -> bool:
    return _sync_lock.locked()


def _slugify(nas_relative_path: str) -> str:
    """Turn a NAS-relative path like "/personal/Documents" into a filesystem-safe
    directory name unique enough not to collide with an unrelated folder."""
    slug = nas_relative_path.strip("/").replace("/", "_")
    slug = re.sub(r"[^a-zA-Z0-9_.-]", "_", slug)
    return slug or "root"


async def get_protected_folders() -> list[dict]:
    return await store.get_value(PROTECTED_FOLDERS_KEY, default=[])


async def get_drive_state() -> dict:
    return await store.get_value(DRIVE_STATE_KEY, default={})


async def save_drive_state(state: dict) -> None:
    await store.set_value(DRIVE_STATE_KEY, state)


async def get_media_enabled() -> bool:
    return await store.get_value(MEDIA_ENABLED_KEY, default=False)


async def set_media_enabled(enabled: bool) -> None:
    await store.set_value(MEDIA_ENABLED_KEY, enabled)


async def get_media_state() -> dict:
    return await store.get_value(MEDIA_STATE_KEY, default={})


async def save_media_state(state: dict) -> None:
    await store.set_value(MEDIA_STATE_KEY, state)


def _media_dest_root(scope: str, owner: Optional[str]) -> Path:
    """Destination directory on the backup drive for one media root — mirrors
    media_reconciler._iter_media_roots()'s (scope, owner) shape so backup and the
    reindexer never disagree about what the media tree actually is."""
    if owner:
        return settings.backup_root / "media" / scope / owner
    return settings.backup_root / "media" / scope


async def try_auto_remount() -> None:
    """On startup, remount a previously-mounted backup drive if the OS-level mount didn't
    survive a reboot — found live 2026-07-15: a reboot leaves the drive genuinely unmounted
    (never added to fstab, since it may not always be attached), but the stored drive state
    keeps claiming it's mounted with nothing to ever correct it, so /local-backup/status lies
    until someone happens to unmount+remount manually. Mirrors storage_routes.try_auto_remount()
    for the primary NAS drive, but deliberately has NO blind-scan fallback if the saved device
    is gone — a backup drive must only ever be the exact device the user explicitly configured,
    never an auto-adopted guess (unlike primary storage, silently backing up to/from the wrong
    drive is a real risk, not just an inconvenience)."""
    state = await get_drive_state()
    device_path = state.get("activeDevice")
    if not device_path:
        return

    backup_root = str(settings.backup_root)
    try:
        with open("/proc/mounts") as fh:
            for line in fh:
                parts = line.split()
                # Compare BOTH the mountpoint and the source device, not just the mountpoint --
                # this service's ProtectSystem=strict + ReadWritePaths=<backup_root> makes systemd
                # bind-mount backup_root onto itself so it's writable inside the service's own
                # private mount namespace. That bind mount shows up in /proc/mounts as a real
                # entry for backup_root REGARDLESS of whether the actual backup device is mounted
                # there or not, so a mountpoint-only check always false-positives "already
                # mounted" from inside this sandboxed process (confirmed live 2026-07-15: this
                # exact bug silently no-op'd every startup, with no log line, since this was the
                # one early-return with nothing logging it -- diagnosed via /proc/<pid>/mounts
                # showing the bind mount's source as the root filesystem device, not the real
                # backup drive).
                if (
                    len(parts) >= 2
                    and parts[1].rstrip("/") == backup_root.rstrip("/")
                    and parts[0] == device_path
                ):
                    logger.info(
                        "local_backup_auto_remount_already_mounted device=%s", device_path,
                    )
                    return
    except OSError:
        pass

    if not Path(device_path).exists():
        logger.warning(
            "local_backup_auto_remount_device_missing device=%s -- clearing stale state",
            device_path,
        )
        await save_drive_state({})
        return

    from .routes.storage_helpers import mount_backup_device
    rc, _, stderr = await mount_backup_device(device_path)
    if rc != 0:
        logger.error("local_backup_auto_remount_failed device=%s error=%s", device_path, stderr)
        return
    logger.info("local_backup_auto_remount_succeeded device=%s", device_path)


def _find_owning_protected_folder(folders: list[dict], nas_relative_path: str) -> Optional[dict]:
    """Return the protected-folder record that owns *nas_relative_path* — the
    longest-matching ancestor (or exact match), so browsing into a subfolder of
    a protected folder still resolves. None if nothing protects this path."""
    target = nas_relative_path.rstrip("/") or "/"
    best: Optional[dict] = None
    best_len = -1
    for folder in folders:
        candidate = folder["path"].rstrip("/") or "/"
        if target == candidate or target.startswith(candidate + "/"):
            if len(candidate) > best_len:
                best = folder
                best_len = len(candidate)
    return best


async def resolve_browse_target(nas_relative_path: str) -> Tuple[Optional[dict], Optional[Path]]:
    """For a requested browse path, return (owning protected-folder record, resolved
    filesystem path inside its `current` snapshot).

    Returns (None, None) if nothing protects this path. Returns (owner, None) if
    the path resolves outside the owning folder's `current` tree (a traversal
    attempt via '..' in the remainder) — distinguished from "not protected" so
    the route can 400 instead of 404.
    """
    folders = await get_protected_folders()
    owner = _find_owning_protected_folder(folders, nas_relative_path)
    if owner is None:
        return None, None

    owner_path = owner["path"].rstrip("/")
    target_path = nas_relative_path.rstrip("/") or "/"
    remainder = target_path[len(owner_path):].lstrip("/")

    current = settings.backup_root / _slugify(owner["path"]) / "current"
    current_resolved = current.resolve()
    if not remainder:
        return owner, current_resolved

    candidate = (current / remainder).resolve()
    if candidate != current_resolved and not str(candidate).startswith(str(current_resolved) + "/"):
        return owner, None
    return owner, candidate


async def _sync_one_folder(entry: dict) -> dict:
    """Run one rsync snapshot for *entry* (a protected_folders record).

    Returns a new dict with lastSyncAt/lastSyncStatus/lastSyncError updated —
    never mutates the input in place, matching this codebase's copy-on-write
    convention for state records.
    """
    result = dict(entry)
    nas_relative = entry["path"]
    src = (settings.nas_root / nas_relative.lstrip("/")).resolve()

    if not src.is_dir():
        result["lastSyncAt"] = datetime.now(timezone.utc).isoformat()
        result["lastSyncStatus"] = "error"
        result["lastSyncError"] = "Source folder no longer exists"
        return result

    slug = _slugify(nas_relative)
    dest_root = settings.backup_root / slug
    snapshots_dir = dest_root / "snapshots"
    current_link = dest_root / "current"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    new_snapshot = snapshots_dir / timestamp

    try:
        snapshots_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        result["lastSyncAt"] = datetime.now(timezone.utc).isoformat()
        result["lastSyncStatus"] = "error"
        result["lastSyncError"] = f"Could not create snapshot directory: {exc}"
        return result

    cmd = ["rsync", "-a", "--delete"]
    if current_link.is_symlink() and current_link.resolve().is_dir():
        cmd.append(f"--link-dest={current_link.resolve()}")
    cmd += [f"{src}/", f"{new_snapshot}/"]

    rc, _, stderr = await run_command(cmd, timeout=_RSYNC_TIMEOUT)
    if rc != 0:
        shutil.rmtree(new_snapshot, ignore_errors=True)
        result["lastSyncAt"] = datetime.now(timezone.utc).isoformat()
        result["lastSyncStatus"] = "error"
        result["lastSyncError"] = stderr.strip()[:500]
        return result

    # Repoint `current` to the new snapshot, then prune old ones beyond retention.
    if current_link.is_symlink():
        current_link.unlink()
    current_link.symlink_to(new_snapshot, target_is_directory=True)
    _prune_old_snapshots(snapshots_dir, keep=_SNAPSHOT_RETENTION)

    result["lastSyncAt"] = datetime.now(timezone.utc).isoformat()
    result["lastSyncStatus"] = "ok"
    result["lastSyncError"] = None
    return result


def _prune_old_snapshots(snapshots_dir: Path, keep: int) -> None:
    """Delete all but the most recent *keep* snapshot directories (by name, which
    is a sortable UTC timestamp)."""
    try:
        snapshots = sorted(p for p in snapshots_dir.iterdir() if p.is_dir())
    except OSError:
        return
    for stale in snapshots[:-keep] if len(snapshots) > keep else []:
        shutil.rmtree(stale, ignore_errors=True)


async def sync_protected_folders() -> dict:
    """Sync every protected folder to the mounted secondary drive.

    Returns a summary dict; never raises — per-folder failures are recorded on
    that folder's own record instead of aborting the whole run.
    """
    if _sync_lock.locked():
        return {"status": "already_syncing", "synced": 0, "failed": 0}

    async with _sync_lock:
        drive_state = await get_drive_state()
        if not drive_state.get("activeDevice"):
            return {"status": "no_drive_mounted", "synced": 0, "failed": 0}

        folders = await get_protected_folders()
        if not folders:
            return {"status": "ok", "synced": 0, "failed": 0}

        updated: list[dict] = []
        synced = 0
        failed = 0
        for entry in folders:
            new_entry = await _sync_one_folder(entry)
            updated.append(new_entry)
            if new_entry.get("lastSyncStatus") == "ok":
                synced += 1
            else:
                failed += 1
                logger.warning(
                    "local_backup_sync_failed path=%s error=%s",
                    entry.get("path"), new_entry.get("lastSyncError"),
                )

        await store.set_value(PROTECTED_FOLDERS_KEY, updated)
        logger.info("local_backup_sync_complete synced=%d failed=%d", synced, failed)
        return {"status": "ok", "synced": synced, "failed": failed}


async def sync_media_library() -> dict:
    """Additively mirror the whole media library (family/personal/entertainment) to the
    mounted secondary drive — see this module's docstring for why additive-mirror, not
    snapshot, is the correct shape for media. Never raises; per-root failures are recorded
    on the shared media-state record rather than aborting the whole run. Shares _sync_lock
    with sync_protected_folders()/restore_media_library() — never two rsyncs at once.
    """
    if _sync_lock.locked():
        return {"status": "already_syncing", "synced": 0, "failed": 0}

    async with _sync_lock:
        if not await get_media_enabled():
            return {"status": "disabled", "synced": 0, "failed": 0}

        drive_state = await get_drive_state()
        if not drive_state.get("activeDevice"):
            return {"status": "no_drive_mounted", "synced": 0, "failed": 0}

        try:
            usage = shutil.disk_usage(str(settings.backup_root))
            if usage.free < _MEDIA_MIN_FREE_BYTES:
                logger.error("local_backup_media_sync_low_space free_bytes=%d", usage.free)
                await save_media_state({
                    "lastSyncAt": datetime.now(timezone.utc).isoformat(),
                    "lastSyncStatus": "error",
                    "lastSyncError": "Backup drive is nearly full — sync skipped",
                })
                return {"status": "low_space", "synced": 0, "failed": 0}
        except OSError:
            pass

        from .media_reconciler import _iter_media_roots
        synced = 0
        failed = 0
        errors: list[str] = []
        for scope, owner, src in _iter_media_roots():
            if not src.is_dir():
                continue
            dest = _media_dest_root(scope, owner)
            try:
                dest.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                failed += 1
                errors.append(f"{scope}/{owner or ''}: {exc}")
                continue

            # -a (archive) implies -rlptgoD, deliberately NOT -H (hardlink preservation,
            # rsync's biggest memory consumer for no benefit on unique media files) and
            # deliberately no --checksum (full-content hashing every file on a 1GB board —
            # the default size+mtime quick-check is the whole point of staying cheap here).
            cmd = ["nice", "-n", "10", "ionice", "-c3", "rsync", "-a", f"{src}/", f"{dest}/"]
            rc, _, stderr = await run_command(cmd, timeout=_MEDIA_RSYNC_TIMEOUT)
            if rc != 0:
                failed += 1
                errors.append(f"{scope}/{owner or ''}: {stderr.strip()[:200]}")
                logger.warning(
                    "local_backup_media_sync_failed scope=%s owner=%s error=%s",
                    scope, owner, stderr.strip()[:200],
                )
            else:
                synced += 1

        status_val = "ok" if failed == 0 else ("partial" if synced else "error")
        await save_media_state({
            "lastSyncAt": datetime.now(timezone.utc).isoformat(),
            "lastSyncStatus": status_val,
            "lastSyncError": "; ".join(errors)[:500] if errors else None,
            "rootsSynced": synced,
            "rootsFailed": failed,
        })
        logger.info("local_backup_media_sync_complete synced=%d failed=%d", synced, failed)
        return {"status": "ok", "synced": synced, "failed": failed}


async def restore_media_library() -> dict:
    """Additively restore the media library FROM the backup drive back TO primary.
    --ignore-existing makes this purely additive on the primary side — it can never
    overwrite or delete a file currently there, so the same command safely covers both
    "recover a few lost files" and "fresh blank drive after disk death" (everything is a
    gap → full copy). Triggers an incremental media_reconciler pass afterward — files on
    disk are invisible to the app until media_index knows about them.
    """
    if _sync_lock.locked():
        return {"status": "already_syncing", "restored": 0, "failed": 0}

    async with _sync_lock:
        drive_state = await get_drive_state()
        if not drive_state.get("activeDevice"):
            return {"status": "no_drive_mounted", "restored": 0, "failed": 0}

        from .media_reconciler import _iter_media_roots
        restored = 0
        failed = 0
        errors: list[str] = []
        for scope, owner, dest in _iter_media_roots():
            src = _media_dest_root(scope, owner)
            if not src.is_dir():
                continue
            try:
                dest.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                failed += 1
                errors.append(f"{scope}/{owner or ''}: {exc}")
                continue

            cmd = [
                "nice", "-n", "10", "ionice", "-c3", "rsync", "-a", "--ignore-existing",
                f"{src}/", f"{dest}/",
            ]
            rc, _, stderr = await run_command(cmd, timeout=_MEDIA_RSYNC_TIMEOUT)
            if rc != 0:
                failed += 1
                errors.append(f"{scope}/{owner or ''}: {stderr.strip()[:200]}")
                logger.warning(
                    "local_backup_media_restore_failed scope=%s owner=%s error=%s",
                    scope, owner, stderr.strip()[:200],
                )
            else:
                restored += 1

    # Reconcile OUTSIDE _sync_lock -- media_reconciler.reconcile_once() is already documented
    # as safe to call concurrently with live traffic / any time, and holding the backup lock
    # through it would needlessly block a legitimate concurrent sync/restore request.
    from .media_reconciler import reconcile_once
    try:
        await reconcile_once()
    except Exception as exc:
        logger.error("local_backup_media_restore_reconcile_failed error=%s", exc)

    logger.info("local_backup_media_restore_complete restored=%d failed=%d", restored, failed)
    return {"status": "ok" if failed == 0 else "partial", "restored": restored, "failed": failed}
