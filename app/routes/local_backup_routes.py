"""
Local Backup routes — selective protected-folder backup to a secondary drive.

Endpoints:
  GET    /api/v1/local-backup/status
  POST   /api/v1/local-backup/mount
  POST   /api/v1/local-backup/unmount
  POST   /api/v1/local-backup/protected-folders
  DELETE /api/v1/local-backup/protected-folders?path=...
  POST   /api/v1/local-backup/sync-now
  GET    /api/v1/local-backup/browse
  GET    /api/v1/local-backup/download
  POST   /api/v1/local-backup/media/enable
  POST   /api/v1/local-backup/media/disable
  POST   /api/v1/local-backup/media/sync-now
  POST   /api/v1/local-backup/restore

Not the same feature as backup_routes.py (phone → NAS auto-backup) — see
local_backup.py's module docstring for the distinction.
"""

import asyncio
import logging
import mimetypes
import os
import urllib.parse
from datetime import datetime, timezone
from shutil import disk_usage

from fastapi import APIRouter, Depends, HTTPException, Query, status
from starlette.responses import Response
from pydantic import BaseModel
from starlette.requests import Request

from .. import local_backup
from ..auth import get_current_user, require_admin
from ..ingest import _resolve_identity
from ..config import settings
from ..job_store import JobStatus, create_job, update_job
from ..models import FileItem, FileListResponse, MountRequest
from .file_routes import stream_file_download
from .storage_helpers import (
    classify_transport,
    find_partition,
    is_os_partition,
    mount_backup_device,
    unmount_backup_device,
)
from .. import store

logger = logging.getLogger("aihomecloud.local_backup")

router = APIRouter(prefix="/api/v1/local-backup", tags=["local-backup"])


class ProtectedFolderRequest(BaseModel):
    path: str  # NAS-relative, e.g. "/personal/Documents"
    label: str = ""


def _resolve_protected_path(nas_relative: str):
    """Resolve *nas_relative* safely within nas_root. Raises HTTPException on
    escape attempts or a target that isn't an existing directory — mirrors
    backup_routes.py's _resolve_dup_path pattern for path-outside-root checks."""
    raw = urllib.parse.unquote(nas_relative)
    nas_root = settings.nas_root.resolve()
    candidate = (settings.nas_root / raw.lstrip("/")).resolve()
    # is_relative_to, not a string prefix. `str(candidate).startswith(str(nas_root))` was the
    # original check and it is not a containment test: "/srv/nas" is a prefix of "/srv/nasty", so
    # a raw path of "../nasty/secrets" resolves to /srv/nasty/secrets and passes. .resolve() above
    # has already collapsed the "..", so the only thing standing between that and a read outside
    # the NAS root was the comparison — and it was the wrong one. The rest of this codebase already
    # uses is_relative_to in ~59 places; these two functions were the outliers.
    # (2026-08-08 audit, adversarial pass.)
    if not candidate.is_relative_to(nas_root):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Path outside NAS root")
    return candidate


@router.get("/status")
async def get_status(user: dict = Depends(get_current_user)) -> dict:
    drive_state = await local_backup.get_drive_state()
    folders = await local_backup.get_protected_folders()

    capacity = None
    if drive_state.get("activeDevice"):
        try:
            usage = disk_usage(str(settings.backup_root))
            capacity = {
                "totalGB": round(usage.total / (1024 ** 3), 1),
                "usedGB": round(usage.used / (1024 ** 3), 1),
            }
        except OSError:
            pass

    media_state = await local_backup.get_media_state()
    return {
        "driveMounted": bool(drive_state.get("activeDevice")),
        "device": drive_state.get("activeDevice"),
        "transport": drive_state.get("transport"),
        "model": drive_state.get("model"),
        "mountedSince": drive_state.get("mountedSince"),
        "capacity": capacity,
        "protectedFolders": folders,
        "syncing": local_backup.is_syncing(),
        "mediaBackup": {
            "enabled": await local_backup.get_media_enabled(),
            "lastSyncAt": media_state.get("lastSyncAt"),
            "lastSyncStatus": media_state.get("lastSyncStatus"),
            "lastSyncError": media_state.get("lastSyncError"),
        },
    }


@router.post("/mount")
async def mount_drive(req: MountRequest, user: dict = Depends(require_admin)) -> dict:
    drive_state = await local_backup.get_drive_state()
    if drive_state.get("activeDevice"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"A backup drive is already mounted ({drive_state['activeDevice']})",
        )

    storage_state = await store.get_storage_state()
    if storage_state.get("activeDevice") == req.device:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "This device is the primary NAS drive — a backup copy on the same "
            "physical disk wouldn't survive that disk failing.",
        )

    target = await find_partition(req.device)
    if not target:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Device {req.device} not found")
    if is_os_partition(target):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Cannot use system storage as a backup drive")
    if not target.get("fstype"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Device has no filesystem — format it first")
    # ext4 specifically, not "any filesystem": local_backup's versioning depends on real POSIX
    # symlinks (the `current` pointer) and chown — found live 2026-07-15 on the Cubie A5E with a
    # pre-existing exFAT drive: the mount itself succeeded, but ahc-mount-nas.sh's chown step
    # fails on exFAT (no real UNIX ownership support), aborting the script and leaving the drive
    # mounted at the OS level while the API call reported failure and never saved drive state —
    # every retry after that failed with "already mounted", permanently stuck with no way to
    # reach this via the app. Rejecting non-ext4 upfront, with a clear message, avoids the whole
    # failure class instead of only handling its one observed symptom.
    if target.get("fstype") != "ext4":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Backup drive must be formatted as ext4 (found {target.get('fstype')}) — "
            "format it from Storage settings first.",
        )

    settings.backup_root.mkdir(parents=True, exist_ok=True)
    rc, _, stderr = await mount_backup_device(req.device)
    if rc != 0:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"Mount failed: {stderr}")

    await local_backup.save_drive_state({
        "activeDevice": req.device,
        "transport": classify_transport(target),
        "model": (target.get("model") or "").strip(),
        "mountedSince": datetime.now(timezone.utc).isoformat(),
    })

    logger.info("local_backup_drive_mounted device=%s", req.device)

    # Opportunistic catch-up: if media backup is enabled, don't make a family wait until
    # 2 AM to catch up after plugging the drive back in (e.g. after it was disconnected for
    # a while). Fire-and-forget, same pattern as sync-now.
    if await local_backup.get_media_enabled():
        asyncio.create_task(local_backup.sync_media_library())

    return {"status": "mounted", "device": req.device}


@router.post("/unmount")
async def unmount_drive(user: dict = Depends(require_admin)) -> dict:
    drive_state = await local_backup.get_drive_state()
    if not drive_state.get("activeDevice"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No backup drive is currently mounted")
    if local_backup.is_syncing():
        raise HTTPException(status.HTTP_409_CONFLICT, "A backup sync is in progress — try again shortly")

    rc, _, stderr = await unmount_backup_device()
    if rc != 0:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"Unmount failed: {stderr}")

    await local_backup.save_drive_state({})
    logger.info("local_backup_drive_unmounted device=%s", drive_state["activeDevice"])
    return {"status": "unmounted", "device": drive_state["activeDevice"]}


@router.post("/protected-folders", status_code=status.HTTP_201_CREATED)
async def add_protected_folder(
    req: ProtectedFolderRequest, user: dict = Depends(require_admin)
) -> dict:
    resolved = _resolve_protected_path(req.path)
    if not resolved.is_dir():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Folder not found")

    nas_relative = "/" + str(resolved.relative_to(settings.nas_root.resolve())).replace("\\", "/")

    def _add(folders: list) -> list:
        if any(f["path"] == nas_relative for f in folders):
            return folders
        folders.append({
            "path": nas_relative,
            "label": req.label or resolved.name,
            "addedAt": datetime.now(timezone.utc).isoformat(),
            "lastSyncAt": None,
            "lastSyncStatus": None,
            "lastSyncError": None,
        })
        return folders

    updated = await store.atomic_update(local_backup.PROTECTED_FOLDERS_KEY, _add, default=[])
    return next(f for f in updated if f["path"] == nas_relative)


@router.delete("/protected-folders", status_code=status.HTTP_204_NO_CONTENT)
async def remove_protected_folder(path: str, user: dict = Depends(require_admin)) -> None:
    nas_relative = path if path.startswith("/") else "/" + path

    def _remove(folders: list) -> list:
        return [f for f in folders if f["path"] != nas_relative]

    before = await local_backup.get_protected_folders()
    after = await store.atomic_update(local_backup.PROTECTED_FOLDERS_KEY, _remove, default=[])
    if len(after) == len(before):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Protected folder not found")


@router.post("/sync-now", status_code=status.HTTP_202_ACCEPTED)
async def sync_now(user: dict = Depends(require_admin)) -> dict:
    if local_backup.is_syncing():
        return {"status": "already_syncing"}
    asyncio.create_task(local_backup.sync_protected_folders())
    return {"status": "started"}


@router.post("/media/enable")
async def enable_media_backup(user: dict = Depends(require_admin)) -> dict:
    """Opt in to whole-media-library backup — a large capacity commitment, so this is a
    deliberate toggle, not something that starts mirroring the instant a drive is plugged
    in. Returns a capacityWarning (does not block) if the backup drive looks too small for
    the current primary library — the real safety net against silently filling the drive
    is sync_media_library()'s own low-space preflight, run before every sync."""
    await local_backup.set_media_enabled(True)

    capacity_warning = None
    try:
        primary_used = disk_usage(str(settings.nas_root)).used
        backup_total = disk_usage(str(settings.backup_root)).total
        if backup_total < primary_used:
            capacity_warning = (
                f"The backup drive ({backup_total // (1024**3)}GB) is smaller than your "
                f"current library ({primary_used // (1024**3)}GB) — not everything may fit."
            )
    except OSError:
        pass

    asyncio.create_task(local_backup.sync_media_library())
    return {"status": "enabled", "capacityWarning": capacity_warning}


@router.post("/media/disable")
async def disable_media_backup(user: dict = Depends(require_admin)) -> dict:
    """Stops future syncs. Deliberately does NOT delete anything already mirrored to the
    backup drive — disabling is not the same as discarding an existing safety copy."""
    await local_backup.set_media_enabled(False)
    return {"status": "disabled"}


@router.post("/media/sync-now", status_code=status.HTTP_202_ACCEPTED)
async def sync_media_now(user: dict = Depends(require_admin)) -> dict:
    if local_backup.is_syncing():
        return {"status": "already_syncing"}
    if not await local_backup.get_media_enabled():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Media backup is not enabled")
    asyncio.create_task(local_backup.sync_media_library())
    return {"status": "started"}


@router.post("/restore", status_code=status.HTTP_202_ACCEPTED)
async def restore_media(user: dict = Depends(require_admin)) -> dict:
    """Additively restore the media library from the backup drive back to primary --
    --ignore-existing in restore_media_library() means this can never overwrite or delete a
    file already on primary, so it's safe to trigger any time a drive has content to offer,
    not just after real data loss. Runs as a background job like smart-activate's format job
    (job_store), polled the same way, since a full-library restore can take a while."""
    drive_state = await local_backup.get_drive_state()
    if not drive_state.get("activeDevice"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No backup drive is currently mounted")
    if local_backup.is_syncing():
        raise HTTPException(status.HTTP_409_CONFLICT, "A backup sync is already in progress")

    job = create_job(user_id=user.get("sub", ""))

    async def _run_restore() -> None:
        update_job(job.id, status=JobStatus.running)
        try:
            result = await local_backup.restore_media_library()
            if result.get("failed"):
                update_job(
                    job.id, status=JobStatus.failed,
                    error=f"{result['failed']} root(s) failed to restore",
                )
            else:
                update_job(job.id, status=JobStatus.completed, result=result)
        except Exception as exc:
            update_job(job.id, status=JobStatus.failed, error=str(exc))
            logger.exception("local_backup_media_restore job failed: %s", exc)

    asyncio.create_task(_run_restore())
    return {"status": "started", "jobId": job.id}


async def _authorize_backup_path(normalized: str, user: dict) -> None:
    """
    Apply the same personal-scope rule to the backup drive as to the primary one.

    /browse and /download reuse resolve_browse_target() for path-safety, which stops traversal but
    says nothing about *whose* files these are. A member could therefore read another member's
    private documents simply by asking the backup copy instead of the original — the same bytes,
    the same privacy promise, no check. Backups are not a lesser copy for access-control purposes.
    """
    low = normalized.lower()
    if not low.startswith("/personal/"):
        return
    name, is_admin = await _resolve_identity(user)
    if is_admin:
        return
    parts = [p for p in normalized.split("/") if p]
    target_owner = parts[1] if len(parts) > 1 else ""
    if target_owner.lower() != (name or "").lower():
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Access to another user's personal files is not allowed",
        )

@router.get("/browse", response_model=FileListResponse)
async def browse_backup(
    path: str = Query("/"),
    user: dict = Depends(get_current_user),
) -> FileListResponse:
    """Read-only listing of what's actually backed up — the secondary drive's
    `current` (latest) snapshot state, shown under the same NAS-relative paths
    as the primary drive. Never exposes the raw snapshots/<timestamp>/
    internals, and has no write counterpart (rename/delete/upload) — editing
    inside a snapshot would corrupt the point-in-time recovery guarantee the
    whole feature rests on. See local_backup.py's resolve_browse_target()."""
    normalized = path if path.startswith("/") else "/" + path
    await _authorize_backup_path(normalized, user)

    if normalized.rstrip("/") in ("", "/"):
        folders = await local_backup.get_protected_folders()
        items = [
            FileItem(
                name=f.get("label") or f["path"].rstrip("/").rsplit("/", 1)[-1],
                path=f["path"].rstrip("/") + "/",
                isDirectory=True,
                sizeBytes=0,
                modified=f.get("lastSyncAt") or f.get("addedAt") or datetime.now(timezone.utc).isoformat(),
                mimeType=None,
            )
            for f in folders
        ]
        return FileListResponse(items=items, totalCount=len(items), page=0, pageSize=max(len(items), 1))

    owner, target = await local_backup.resolve_browse_target(normalized)
    if owner is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not a protected folder")
    if target is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Path outside protected folder")
    if not target.exists():
        # Not synced yet, or the source was removed after the last successful sync —
        # an empty listing (not an error) matches "this folder is empty" in the UI.
        return FileListResponse(items=[], totalCount=0, page=0, pageSize=1)
    if not target.is_dir():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Path is not a directory")

    # Prefix with the path actually being browsed, not the owning protected folder's own path —
    # browsing into a nested subfolder must keep every intermediate segment (found live 2026-07-15:
    # using owner_path here silently dropped everything between the protected folder and the
    # browsed subfolder, e.g. ".../subdir/inner.txt" came back as ".../inner.txt").
    browse_prefix = normalized.rstrip("/")
    items: list[FileItem] = []
    try:
        with os.scandir(target) as it:
            for entry in it:
                try:
                    st = entry.stat(follow_symlinks=False)
                    is_dir = entry.is_dir(follow_symlinks=False)
                    nas_path = f"{browse_prefix}/{entry.name}" if browse_prefix else f"/{entry.name}"
                    if is_dir:
                        nas_path += "/"
                    mime, _ = mimetypes.guess_type(entry.name)
                    items.append(FileItem(
                        name=entry.name,
                        path=nas_path,
                        isDirectory=is_dir,
                        sizeBytes=0 if is_dir else st.st_size,
                        modified=datetime.fromtimestamp(st.st_mtime, tz=timezone.utc),
                        mimeType=mime,
                    ))
                except OSError:
                    continue
    except OSError:
        pass

    items.sort(key=lambda i: (not i.is_directory, i.name.casefold()))
    return FileListResponse(items=items, totalCount=len(items), page=0, pageSize=max(len(items), 1))


@router.get("/download",
    responses={200: {"content": {"application/octet-stream": {}}}},
    response_class=Response,
)
async def download_backup_file(
    request: Request,
    path: str = Query(..., description="NAS-relative path (as returned by /browse) to download"),
    user: dict = Depends(get_current_user),
):
    """Download/stream a file from the backup drive's `current` snapshot — same Range-request
    support as the main NAS /files/download route (see stream_file_download's docstring), reusing
    resolve_browse_target() for the identical safety guarantees /browse already has (never exposes
    raw snapshots/<timestamp>/ internals, 404 for anything outside a protected folder)."""
    normalized = path if path.startswith("/") else "/" + path
    await _authorize_backup_path(normalized, user)
    owner, target = await local_backup.resolve_browse_target(normalized)
    if owner is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not a protected folder")
    if target is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Path outside protected folder")
    if not target.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found")
    if target.is_dir():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Cannot download a directory")

    return stream_file_download(request, target)
