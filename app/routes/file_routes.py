"""
File management routes — list, mkdir, delete, rename, upload, download.
All paths are sandboxed under settings.nas_root.
External storage must be mounted at nas_root; SD card fallback is blocked.
"""

import asyncio
import hashlib
import io
import logging
import mimetypes
import re
import tempfile
import zipfile
from urllib.parse import quote
import os
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from functools import partial
from pathlib import Path

from PIL import Image, ImageOps

from fastapi import APIRouter, Depends, Header, HTTPException, UploadFile, File, Query, status
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

from ..auth import get_current_user, require_admin
from ..audit import audit_log
from starlette.concurrency import run_in_threadpool

from ..models import (
    DocumentSearchResponse,
    MkdirResponse,
    ReindexCancelledResponse,
    SemanticIndexStatusResponse,
    SemanticSearchResponse,
    StorageRootsResponse,
)
from ..models import (ReindexStartedResponse, UploadPrecheckRequest,
                      UploadPrecheckResponse, UploadResponse)
from .. import upload_idempotency
from ..config import settings
from ..models import (
    CategoryStatItem,
    CategoryStatsResponse,
    CreateFolderRequest,
    FileItem,
    FileListResponse,
    RenameRequest,
)
from .. import store
from ..job_store import JobStatus, create_job, update_job
from ..file_sorter import _destination_folder
from ..events import file_event_bus, FileEvent
from ..limiter import limiter
from ..ingest import (
    BLOCKED_EXTENSIONS,
    Destination,
    IngestDiskFullError,
    IngestMode,
    IngestSizeError,
    IngestStallError,
    Scope,
    _authorize_path,
    _require_external_storage,
    _resolve_identity,
    _safe_resolve,
    ingest,
)
from .. import media_index
from .event_routes import emit_upload_complete

logger = logging.getLogger("aihomecloud.files")

# Short-lived scandir result cache.  Key: "<resolved_dir>|<sort_by>|<sort_dir>|<page>|<page_size>"
# Value: (result_tuple, expires_at_monotonic)
import time as _time
_scan_cache: dict[str, tuple] = {}
_SCAN_TTL = 7.0        # seconds
_SCAN_CACHE_MAX = 500  # maximum entries; prevents unbounded growth on busy NAS

_THUMB_IMAGE_EXTS: frozenset[str] = frozenset({
    ".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".tiff", ".tif", ".gif"
})
_THUMB_VIDEO_EXTS: frozenset[str] = frozenset({
    ".mp4", ".mov", ".m4v", ".3gp", ".mkv", ".avi", ".webm"
})
_THUMB_FFMPEG: str = "/usr/bin/ffmpeg"
_THUMB_MAX_AGE: int = 86400

# Deployment target is a 1GB-RAM SBC — an uncapped default ThreadPoolExecutor lets many
# full-resolution image decodes / ffmpeg frame extractions run concurrently (e.g. scrolling a
# large photo grid), each raising the process's RSS high-water-mark, which CPython/glibc never
# return to the OS. These caps bound how many can be in flight at once. Images get 2 (draft-mode
# decode below keeps each one small); video frame extraction gets 1 (a single 4K/HEVC frame grab
# is already 50-150MB transient — two at once risks OOM on the target hardware).
_THUMB_IMAGE_SEMAPHORE = asyncio.Semaphore(2)
_THUMB_VIDEO_SEMAPHORE = asyncio.Semaphore(1)



def _invalidate_scan_cache(dir_path: str) -> None:
    """Remove every cache entry for the given directory."""
    prefix = dir_path + "|"
    to_del = [k for k in _scan_cache if k.startswith(prefix)]
    for k in to_del:
        _scan_cache.pop(k, None)


def _evict_expired_scan_cache() -> None:
    """Remove expired entries; then evict oldest if over the size cap."""
    now = _time.monotonic()
    stale = [k for k, (_, exp) in _scan_cache.items() if now >= exp]
    for k in stale:
        _scan_cache.pop(k, None)
    # Size cap: drop the oldest insertion-order entries until under the limit.
    while len(_scan_cache) >= _SCAN_CACHE_MAX:
        _scan_cache.pop(next(iter(_scan_cache)))


def _calc_dir_size(path: Path) -> int:
    """
    Total size of every file under a directory. Runs in a thread executor.

    The 2026-07-30 files finding 4 claimed this "crashes uncaught on PermissionError" from
    `rglob`. Checked directly on Python 3.12: **it does not.** `rglob` silently skips a
    directory it cannot read and completes normally. The real behaviour is the opposite of
    the report — not a crash, a silent UNDER-COUNT, with the unreadable subtree contributing
    zero and nothing anywhere saying so.

    `os.walk` is used instead of `rglob` precisely because it can report that, via `onerror`.
    The returned total is unchanged in the normal case; when part of the tree is unreadable
    the shortfall is logged, so a surprising trash size has something to explain it.

    Not raised as an error: the delete itself still succeeds (`shutil.move` renames the parent
    directory regardless of a child's permissions), so failing the operation over an inaccurate
    byte count would break something that works today to fix a number.
    """
    total = 0
    unreadable: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(path, onerror=lambda e: unreadable.append(str(e.filename))):
        for name in filenames:
            try:
                fp = os.path.join(dirpath, name)
                if not os.path.islink(fp):
                    total += os.stat(fp).st_size
            except OSError:
                # An entry vanishing mid-walk (a race with another writer) is not a reason to
                # abandon the whole calculation.
                continue
    if unreadable:
        logger.warning(
            "dir_size_undercounted path=%s unreadable_subtrees=%d first=%s",
            path, len(unreadable), unreadable[0],
        )
    return total


def _calc_dir_stats(path: Path) -> tuple[int, int]:
    """Combined file count + total size for a directory in one rglob pass -- used by
    /category-stats, which needs both numbers per category, not just one. A category folder that
    hasn't been auto-sorted into yet (doesn't exist) reports (0, 0) rather than erroring."""
    if not path.is_dir():
        return 0, 0
    count = 0
    total = 0
    for f in path.rglob("*"):
        if f.is_file() and not f.is_symlink():
            count += 1
            total += f.stat().st_size
    return count, total


def _calc_dir_size_capped(path: Path, cap: int) -> int:
    """Like _calc_dir_size, but stops walking as soon as the running total exceeds cap --
    download_folder_zip only needs to know "over the limit or not", not the exact total, so a
    large/shared folder doesn't pay for a full recursive stat() pass just to get rejected.
    The returned value is >= the real total whenever it exceeds cap (exact otherwise)."""
    total = 0
    for f in path.rglob("*"):
        if f.is_file() and not f.is_symlink():
            total += f.stat().st_size
            if total > cap:
                return total
    return total


router = APIRouter(prefix="/api/v1/files", tags=["files"])

# BLOCKED_EXTENSIONS, _require_external_storage, _safe_resolve, _resolve_identity,
# and _authorize_path now live in ..ingest (imported above) so both this module
# and the ingest core share one copy. Re-exported at module level (via the import
# above) for existing external importers: trash_routes.py, web_browser_routes.py,
# auth_routes.py, telegram_upload_routes.py.


def _is_documents_scoped(path: Path) -> bool:
    """True when path is in a Documents folder (or is that folder)."""
    return path.name == "Documents" or "Documents" in path.parts


def _is_document_file(path: Path) -> bool:
    from ..document_index import is_indexable_document_path

    return _is_documents_scoped(path) and is_indexable_document_path(path)


def _destination_from_dir(resolved_dir: Path) -> Destination:
    """Convert an already-authorized, already-resolved NAS directory into an
    ingest Destination that writes to that EXACT directory (MIRROR — no
    auto-sort, matching pre-unification behavior for direct-path uploads).

    `raw_dir` carries the resolved directory through untouched rather than
    reconstructing a path from a fixed scope enum — some callers (tests,
    admin tools) upload to arbitrary non-bucket directories under nas_root,
    and re-deriving the path from `scope` would silently redirect them.
    `scope` is best-effort, used only to pick the right dedup-index bucket:
    PERSONAL for anything under personal/<owner>/ (so dedup respects the
    owner boundary), FAMILY for everything else (family/entertainment/or
    any other shared directory)."""
    rel_parts = resolved_dir.relative_to(settings.nas_root.resolve()).parts
    if not rel_parts:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Cannot upload to NAS root")
    if rel_parts[0] == "personal" and len(rel_parts) > 1:
        return Destination(
            scope=Scope.PERSONAL, owner=rel_parts[1], raw_dir=resolved_dir, mode=IngestMode.MIRROR,
        )
    if rel_parts[0] == "entertainment":
        return Destination(scope=Scope.ENTERTAINMENT, raw_dir=resolved_dir, mode=IngestMode.MIRROR)
    return Destination(scope=Scope.FAMILY, raw_dir=resolved_dir, mode=IngestMode.MIRROR)


async def _resolve_upload_destination(
    path: str, sync_scope: str | None, source_folder: str | None,
    user: dict, safe_username: str,
) -> tuple[Destination, str | None, str | None]:
    """Resolve an /upload or /upload-stream request into (dest, source, source_folder).

    Three mutually exclusive request shapes, checked in this order:
    1. `path` given → exact-directory MIRROR write (general "upload to this folder"
       feature — browse, tap upload here). `syncScope`/`sourceFolder` are ignored if
       also present; an explicit path always wins.
    2. `syncScope` given (no `path`) → phone sync. Retired physical `Backups/<id>/`
       tree: sync now writes through the same SORTED, bucket-allocated tree as a
       direct upload — merged with direct uploads, and for family scope, merged
       across every family member who syncs there — with source="sync" recorded
       explicitly rather than inferred from a physical path shape.
    3. Neither given → default personal SORTED upload (unchanged prior behavior).
    """
    if path and path.strip():
        resolved_dir = _safe_resolve(path)
        await _authorize_path(resolved_dir, user)
        # No resolved_dir.is_dir() gate: ingest() already mkdir(parents=True,
        # exist_ok=True)s the destination itself, so requiring the directory
        # to pre-exist here just silently discarded a perfectly valid,
        # explicitly-requested destination on its first-ever upload and fell
        # back to the personal/SORTED default instead, with no error surfaced.
        return _destination_from_dir(resolved_dir), None, None

    if sync_scope:
        if sync_scope == "personal":
            return (
                Destination(scope=Scope.PERSONAL, owner=safe_username, mode=IngestMode.SORTED),
                "sync", source_folder,
            )
        if sync_scope == "family":
            return (
                Destination(scope=Scope.FAMILY, mode=IngestMode.SORTED),
                "sync", source_folder,
            )
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid syncScope '{sync_scope}'")

    return Destination(scope=Scope.PERSONAL, owner=safe_username, mode=IngestMode.SORTED), None, None


def _file_item(p: Path, rel_prefix: str) -> dict:
    """Convert a Path into a FileItem-compatible dict."""
    stat = p.stat()
    is_dir = p.is_dir()
    name = p.name
    # Rebuild NAS-style path: /srv/nas/...
    nas_path = "/" + str(p.relative_to(settings.nas_root.resolve())).replace("\\", "/")
    if is_dir and not nas_path.endswith("/"):
        nas_path += "/"

    mime, _ = mimetypes.guess_type(name)

    return {
        "name": name,
        "path": nas_path,
        "isDirectory": is_dir,
        "sizeBytes": 0 if is_dir else stat.st_size,
        "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        "mimeType": mime,
    }


_YEAR_DIR_RE = re.compile(r"^\d{4}$")
_MONTH_DIR_RE = re.compile(r"^\d{2}(-p\d+)?$")


def _flatten_date_bucket_dirs(resolved: Path, nas_root_str: str, year_dir_names: set[str]) -> list[dict]:
    """Walk each <year_dir_names>/MM(-pN)/ subtree under *resolved* and return
    its files as a flat list, as if they were direct children of *resolved*.

    Why this exists: ingest() now nests SORTED-mode writes under
    <category>/<YYYY>/<MM>/ by capture date (see app/ingest.py), so a plain
    scandir of a category root (e.g. personal/<user>/Photos/) only sees year
    directories, not the photos themselves. Callers that still browse by
    physical category path — the Android Photos/Videos "All" grid's
    paginated/sorted listMediaPage, not yet switched to the media_index query
    endpoint — would otherwise show nothing the moment any file lands in a
    date bucket. This keeps /files/list itself backward-compatible without
    requiring that grid's pagination model to change. Bounded and cheap: a
    year has at most ~12 month dirs plus rare overflow parts.
    """
    files: list[dict] = []
    for year_name in year_dir_names:
        year_path = resolved / year_name
        try:
            month_dirs = [e for e in os.scandir(year_path) if e.is_dir(follow_symlinks=False)]
        except OSError:
            continue
        for month_entry in month_dirs:
            if not _MONTH_DIR_RE.match(month_entry.name):
                continue
            try:
                with os.scandir(month_entry.path) as it:
                    for entry in it:
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        try:
                            st = entry.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        rel = os.path.relpath(entry.path, nas_root_str)
                        nas_path = "/" + rel.replace("\\", "/")
                        mime, _ = mimetypes.guess_type(entry.name)
                        files.append({
                            "name": entry.name,
                            "path": nas_path,
                            "isDirectory": False,
                            "sizeBytes": st.st_size,
                            "modified": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
                            "mimeType": mime,
                        })
            except OSError:
                continue
    return files


def _scandir_list(resolved: Path, nas_root_str: str, sort_key: str, reverse: bool, page: int, page_size: int) -> tuple:
    """Run in thread pool: scandir + sort + paginate without blocking the event loop.

    Uses os.scandir() which is significantly faster than Path.iterdir() + stat()
    because it reads directory entries and their stat info in a single syscall
    per entry (on Linux, via getdents + fstatat cached in the dirent).
    """
    entries = []
    try:
        with os.scandir(resolved) as it:
            for entry in it:
                try:
                    st = entry.stat(follow_symlinks=False)
                    is_dir = entry.is_dir(follow_symlinks=False)
                    name = entry.name
                    rel = os.path.relpath(entry.path, nas_root_str)
                    nas_path = "/" + rel.replace("\\", "/")
                    if is_dir and not nas_path.endswith("/"):
                        nas_path += "/"
                    mime, _ = mimetypes.guess_type(name)
                    entries.append({
                        "name": name,
                        "path": nas_path,
                        "isDirectory": is_dir,
                        "sizeBytes": 0 if is_dir else st.st_size,
                        "modified": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
                        "mimeType": mime,
                    })
                except OSError:
                    continue
    except OSError as e:
        # An unmounted drive or a permission change mid-browse used to render as "Empty
        # folder" with no signal that anything went wrong. The return contract is left alone
        # (callers and the UI both expect a list, and changing it is a UI change beyond this
        # fix) but it is no longer silent — this is the only record that the folder was
        # unreadable rather than empty. (2026-07-30 files finding 5.)
        logger.warning("scandir_failed path=%s error=%s", resolved, e)

    year_dir_names = {e["name"] for e in entries if e["isDirectory"] and _YEAR_DIR_RE.match(e["name"])}
    if year_dir_names:
        loose_files = [e for e in entries if not e["isDirectory"]]
        entries = loose_files + _flatten_date_bucket_dirs(resolved, nas_root_str, year_dir_names)

    # Sort
    if sort_key == "modified":
        entries.sort(key=lambda i: i.get("modified") or "", reverse=reverse)
    elif sort_key == "size":
        entries.sort(key=lambda i: i.get("sizeBytes") or 0, reverse=reverse)
    else:
        entries.sort(key=lambda i: ((i["name"] or "").casefold(), i["name"] or ""), reverse=reverse)

    total_count = len(entries)
    start = page * page_size
    paged = entries[start:start + page_size]
    return paged, total_count


@router.get("/list", response_model=FileListResponse)
@limiter.limit("60/minute")
async def list_files(
    request: Request,
    path: str = Query("/srv/nas/family/"),
    page: int = Query(0, ge=0),
    page_size: int = Query(50, ge=1, le=500, alias="page_size"),
    sort_by: str = Query("name"),
    sort_dir: str = Query("asc"),
    user: dict = Depends(get_current_user),
):
    """List files and folders at the given NAS path."""
    _require_external_storage()
    resolved = _safe_resolve(path)
    await _authorize_path(resolved, user)

    if not resolved.exists():
        resolved.mkdir(parents=True, exist_ok=True)

    if not resolved.is_dir():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Path is not a directory")

    loop = asyncio.get_running_loop()
    nas_root_str = str(settings.nas_root.resolve())
    cache_key = f"{str(resolved)}|{sort_by}|{sort_dir}|{page}|{page_size}"
    now = _time.monotonic()
    cached = _scan_cache.get(cache_key)
    if cached and now < cached[1]:
        paged, total_count = cached[0]
    else:
        paged, total_count = await loop.run_in_executor(
            None,
            partial(_scandir_list, resolved, nas_root_str,
                    sort_by.lower(), sort_dir.lower() == "desc",
                    page, page_size),
        )
        _evict_expired_scan_cache()
        _scan_cache[cache_key] = ((paged, total_count), now + _SCAN_TTL)

    # Attach the stable media identity so a client can report playback position without ever
    # keying on the path. One batched query for the page, not one per file. Files the indexer has
    # not reached yet simply get null, and take no part in server-side resume — deliberately no
    # path-based fallback, since replacing path-as-identity is the whole point.
    #
    # Deliberately NOT inside the _scan_cache entry above: that cache is keyed on the directory
    # scan, and an entry id can appear as soon as the indexer runs. Caching it would keep serving
    # null for the cache's lifetime after a file became resumable.
    entry_ids = await media_index.entry_ids_for_paths(
        [i["path"] for i in paged if not i.get("isDirectory")]
    )

    return FileListResponse(
        items=[FileItem(**i, entryId=entry_ids.get(i["path"])) for i in paged],
        totalCount=total_count,
        page=page,
        pageSize=page_size,
    )


@router.get("/category-stats", response_model=CategoryStatsResponse)
@limiter.limit("30/minute")
async def category_stats(
    request: Request,
    path: list[str] = Query(..., description="One or more NAS paths to compute file count + total size for"),
    user: dict = Depends(get_current_user),
):
    """Per-path file count + total size, e.g. for the Files home screen's category rows (Photos/
    Videos/Documents/Music/Others). A path that hasn't been auto-sorted into yet (doesn't exist)
    reports zero rather than 404 -- an empty category is a normal, expected state here, not an
    error. Reuses the same short-lived scan cache as /list so repeated Files-tab visits within a
    few seconds don't each pay a fresh recursive walk."""
    _require_external_storage()
    loop = asyncio.get_running_loop()

    async def _stats_for(raw_path: str) -> CategoryStatItem:
        resolved = _safe_resolve(raw_path)
        await _authorize_path(resolved, user)
        cache_key = f"stats|{resolved}"
        now = _time.monotonic()
        cached = _scan_cache.get(cache_key)
        if cached and now < cached[1]:
            count, total = cached[0]
        else:
            count, total = await loop.run_in_executor(None, _calc_dir_stats, resolved)
            _evict_expired_scan_cache()
            _scan_cache[cache_key] = ((count, total), now + _SCAN_TTL)
        return CategoryStatItem(path=raw_path, file_count=count, total_bytes=total)

    results = await asyncio.gather(*(_stats_for(p) for p in path))
    return CategoryStatsResponse(categories=list(results))


@router.post("/mkdir", status_code=status.HTTP_201_CREATED, response_model=MkdirResponse)
@limiter.limit("60/minute")
async def create_folder(request: Request, body: CreateFolderRequest, user: dict = Depends(get_current_user)):
    """Create a new directory."""
    _require_external_storage()
    resolved = _safe_resolve(body.path)
    await _authorize_path(resolved, user)
    if resolved.exists():
        raise HTTPException(status.HTTP_409_CONFLICT, "Folder already exists")
    resolved.mkdir(parents=True, exist_ok=True)
    _invalidate_scan_cache(str(resolved.parent))
    return {"path": body.path}


async def _soft_delete_resolved(resolved: Path, original_path: str, user_id: str) -> None:
    """Move an already-authorized, already-resolved file/dir into the
    per-user trash and record it, identically for every caller — both the
    path-based /files/delete route and the entry-id-based /media/{id} DELETE
    route (media_routes.py) funnel through this so soft-delete behavior can't
    drift between the two."""
    if not resolved.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")

    filename = resolved.name
    ts = int(datetime.now(timezone.utc).timestamp())
    trash_name = f"{ts}_{filename}"

    # Per-user trash directory: {nas_root}/.ahc_trash/{user_id}/
    user_trash_dir = settings.trash_dir / user_id
    user_trash_dir.mkdir(parents=True, exist_ok=True)

    # Guard against name collision inside trash
    trash_path = user_trash_dir / trash_name
    counter = 1
    while trash_path.exists():
        trash_path = user_trash_dir / f"{ts}_{counter}_{filename}"
        counter += 1

    # Calculate size before moving (also remembered below — `resolved` no longer
    # exists as a file once shutil.move below has run).
    was_file = resolved.is_file()
    if was_file:
        size_bytes = resolved.stat().st_size
    else:
        loop = asyncio.get_running_loop()
        size_bytes = await loop.run_in_executor(None, _calc_dir_size, resolved)

    item = {
        "id": str(uuid.uuid4()),
        "originalPath": original_path,
        "trashPath": str(trash_path),
        "filename": filename,
        "deletedAt": datetime.now(timezone.utc).isoformat(),
        "sizeBytes": size_bytes,
        "deletedBy": user_id,
    }

    # Write metadata FIRST — if the move fails we can roll it back cleanly. Uses the atomic
    # add/remove helpers (not get_trash_items()+save_trash_items(), which each lock
    # individually and lose entries under concurrent soft-deletes — found live 2026-07-30 via
    # Stage 2's file-access council review).
    await store.add_trash_item(item)

    try:
        shutil.move(str(resolved), str(trash_path))
    except Exception:
        await store.remove_trash_item(item["id"])
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Failed to move file to trash")

    _invalidate_scan_cache(str(resolved.parent))

    # Keep media_index in sync with soft-delete operations — without this, a
    # path-based delete (this route has no entry id to call
    # media_index.mark_entry_deleted() with) left media_index unaware the file
    # was gone until the next reconcile pass. This single flip also clears the
    # dedup record: ingest's duplicate lookup reads live media_index entries
    # directly (the separate ingest_hashes store is gone), so a future
    # re-upload of identical content is correctly treated as new. Harmless
    # no-op for directories (no single blob).
    if was_file:
        await media_index.mark_entry_deleted_by_rel_path(original_path)

    # Keep document index in sync with soft-delete operations.
    from ..document_index import remove_document, remove_documents_by_prefix
    if resolved.is_file() and _is_documents_scoped(resolved):
        await remove_document(original_path)
    elif resolved.is_dir() and _is_documents_scoped(resolved):
        await remove_documents_by_prefix(original_path)

    audit_log("file_deleted", actor_id=user_id, path=original_path, file_name=filename, size_bytes=size_bytes)

    # Publish file-event for downstream consumers (AI features, audit log)
    await file_event_bus.publish(FileEvent(
        path=original_path,
        action="delete",
        user=user_id,
    ))

    # Run trash purge in background — don't block the delete response
    from .trash_routes import _safe_purge_trash
    asyncio.create_task(_safe_purge_trash())


@router.delete("/delete", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("60/minute")
async def delete_file(
    request: Request,
    path: str = Query(...),
    user: dict = Depends(get_current_user),
):
    """Soft-delete: move file/directory to the per-user trash folder."""
    _require_external_storage()
    resolved = _safe_resolve(path)
    await _authorize_path(resolved, user)
    await _soft_delete_resolved(resolved, path, user.get("sub", ""))


@router.put("/rename", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("60/minute")
async def rename_file(request: Request, body: RenameRequest, user: dict = Depends(get_current_user)):
    """Rename a file or directory."""
    _require_external_storage()
    if not body.new_name.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Name cannot be empty")

    # Reject names containing path separators to prevent traversal via rename
    safe_new_name = Path(body.new_name).name
    if safe_new_name != body.new_name.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid file name")
    if not safe_new_name or safe_new_name in (".", ".."):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid file name")
    # Block renaming to dangerous extensions
    rename_suffixes = [s.lower() for s in Path(safe_new_name).suffixes]
    for ext in rename_suffixes:
        if ext in BLOCKED_EXTENSIONS:
            raise HTTPException(
                status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                f"Renaming to '{ext}' is not allowed for security reasons.",
            )

    resolved = _safe_resolve(body.old_path)
    await _authorize_path(resolved, user)
    if not resolved.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")

    new_path = resolved.parent / safe_new_name
    # Verify new path is still inside NAS root
    nas_resolved = settings.nas_root.resolve()
    try:
        new_path.resolve().relative_to(nas_resolved)
    except ValueError:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Path outside NAS root")
    if new_path.exists():
        raise HTTPException(status.HTTP_409_CONFLICT, "A file with that name already exists")

    was_file = resolved.is_file()
    was_dir = resolved.is_dir()
    resolved.rename(new_path)
    _invalidate_scan_cache(str(resolved.parent))

    # Keep index paths aligned after rename/move.
    from ..document_index import (
        index_document,
        index_documents_under_path,
        remove_document,
        remove_documents_by_prefix,
    )
    added_by = user.get("sub", "unknown")
    if was_file and (_is_documents_scoped(resolved) or _is_documents_scoped(new_path)):
        await remove_document(str(resolved))
        if _is_document_file(new_path):
            await index_document(str(new_path), new_path.name, added_by)
    elif was_dir and (_is_documents_scoped(resolved) or _is_documents_scoped(new_path)):
        await remove_documents_by_prefix(str(resolved))
        if _is_documents_scoped(new_path):
            await index_documents_under_path(str(new_path), added_by)


# --- Upload idempotency -----------------------------------------------------
# A dropped connection cannot be told apart from a rejected upload by the client,
# so it retries and the NAS gains a duplicate. An optional client-minted UUIDv7
# lets the server recognise the retry. Absent the header, behaviour is exactly as
# before — this is additive, and old clients are unaffected.

def _idempotency_begin(key: str | None, user_sub: str) -> dict | None:
    """The response to replay, or None if this caller should perform the upload."""
    if key is None:
        return None
    if not upload_idempotency.is_valid_key(key):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Idempotency-Key must be a UUIDv7 (its embedded timestamp is what bounds retention)",
        )
    outcome, response = upload_idempotency.begin(key, user_sub)
    if outcome == "replayed":
        return response
    if outcome == "in_progress":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "An upload with this Idempotency-Key is already in progress",
        )
    return None


def _idempotency_finish(key: str | None, response: dict) -> None:
    if key is not None:
        upload_idempotency.finish(key, response)


def _idempotency_abandon(key: str | None) -> None:
    if key is not None:
        upload_idempotency.abandon(key)


@router.post("/upload/precheck", response_model=UploadPrecheckResponse)
@limiter.limit("60/minute")
async def upload_precheck(
    request: Request,
    body: UploadPrecheckRequest,
    user: dict = Depends(get_current_user),
):
    """
    Ask which files are actually needed, before sending any bytes.

    Borrowed from LocalSend's `prepare-upload` handshake (protocol spec is public; this is a
    re-implementation, not their code — LocalSend is AGPL-3.0). The insight is that the sender
    already knows every file's hash, so the receiver can answer "skip these" in **one** round trip.

    Without it, a duplicate costs a full network transfer, a full disk write with `fsync` and a
    full SHA-256 on the board, and is only then recognised and deleted. Measured on the dev board,
    that is ~330 ms and ~800 KB of traffic thrown away per already-present file. For a phone
    re-syncing a camera roll, most files are already-present.

    Answers only for the caller's own scope: `personal` is filtered by owner, so one member cannot
    probe another's library by hash. That mirrors the single-item dedup rule exactly — relaxing it
    here would turn a private lookup into an oracle.
    """
    from .. import media_index

    name, _is_admin = await _resolve_identity(user)
    scope = "family" if body.syncScope == "family" else "personal"
    owner = None if scope == "family" else name

    hashes = [h.strip().lower() for h in body.hashes if h and h.strip()]
    present = await media_index.find_live_by_hashes(scope, owner, hashes)
    needed = [h for h in dict.fromkeys(hashes) if h not in present]
    return {
        "needed": needed,
        "haveCount": len(present),
        "checked": len(dict.fromkeys(hashes)),
    }


@router.post("/upload", status_code=status.HTTP_201_CREATED, response_model=UploadResponse)
@limiter.limit(lambda: settings.upload_rate_limit)
async def upload_file(
    request: Request,
    path: str = Query("", description="Destination directory (NAS-absolute). If empty, falls back to user's .inbox/ for auto-sorting."),
    syncScope: str | None = Query(None, description="'personal' or 'family' — phone Sync uploads use this instead of `path`, landing in the normal SORTED/date-bucketed tree (merged with direct uploads) rather than a physically distinct location."),
    sourceFolder: str | None = Query(None, description="Sync folder id, recorded as media_index metadata when syncScope is given."),
    original_date: float | None = Query(None, description="Client-supplied original capture time (epoch seconds), used when the file has no embedded date."),
    file: UploadFile = File(...),
    user: dict = Depends(get_current_user),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key", description="Optional client-minted UUIDv7, stable across retries of the same logical upload. Retrying with the same key replays the original result instead of storing the file twice."),
):
    """
    Upload a file via multipart form data.
    When *path* points to a valid NAS directory the file is written there directly
    (no auto-sort). When *syncScope* is given (Sync), the file is classified and
    date-bucketed the same as a direct upload, into the shared scope tree. When
    neither is given, the file is synchronously classified into Photos/Videos/
    Documents/Others under the user's personal folder — sorting happens at ingest
    time, not via a best-effort background watcher.
    """
    if user.get("type") == "device":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Device tokens cannot upload files")

    user_record = await store.find_user(user.get("sub", ""))
    if user_record is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "User not found")
    safe_username = Path(user_record["name"]).name

    replay = _idempotency_begin(idempotency_key, user.get("sub", ""))
    if replay is not None:
        return replay

    committed = False
    try:
        dest, upload_source, upload_source_folder = await _resolve_upload_destination(
            path, syncScope, sourceFolder, user, safe_username,
        )

        async def _chunks():
            while chunk := await file.read(settings.upload_chunk_size):
                yield chunk

        try:
            result = await ingest(
                _chunks(), filename=file.filename or "upload", dest=dest, user=user,
                original_date=original_date, source=upload_source, source_folder=upload_source_folder,
            )
        except IngestStallError:
            raise HTTPException(status.HTTP_408_REQUEST_TIMEOUT, "Upload stalled — no data received")
        except IngestSizeError as e:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, str(e))
        except IngestDiskFullError as e:
            raise HTTPException(status.HTTP_507_INSUFFICIENT_STORAGE, str(e))

        safe_name = result.path.name
        user_name = user.get("sub", "unknown")
        asyncio.create_task(_post_upload_notify(safe_name, user_name, str(result.path)))
        _invalidate_scan_cache(str(result.path.parent))

        if _is_document_file(result.path):
            from ..document_index import index_document

            asyncio.create_task(
                index_document(str(result.path), safe_name, user_name),
                name=f"index_upload_{safe_name}",
            )

        if result.path.suffix.lower() in _THUMB_VIDEO_EXTS:
            asyncio.create_task(
                _pregenerate_video_thumbnail(result.path),
                name=f"thumb_pregenerate_{safe_name}",
            )

        _resp = {
            "name": safe_name,
            "path": "/" + str(result.path.relative_to(settings.nas_root.resolve())).replace("\\", "/"),
            "sizeBytes": result.bytes_written,
            "sortedTo": result.sorted_to,
        }
        _idempotency_finish(idempotency_key, _resp)
        committed = True
        return _resp
    finally:
        if not committed:
            _idempotency_abandon(idempotency_key)


@router.post("/upload-stream", status_code=status.HTTP_201_CREATED, response_model=UploadResponse)
@limiter.limit("120/minute")
async def upload_file_stream(
    request: Request,
    filename: str = Query(..., min_length=1, max_length=512),
    path: str = Query("", description="Destination directory (NAS-absolute). Empty → user .inbox/."),
    syncScope: str | None = Query(None, description="'personal' or 'family' — phone Sync uploads use this instead of `path`, landing in the normal SORTED/date-bucketed tree (merged with direct uploads) rather than a physically distinct location."),
    sourceFolder: str | None = Query(None, description="Sync folder id, recorded as media_index metadata when syncScope is given."),
    original_date: float | None = Query(None, description="Client-supplied original capture time (epoch seconds)."),
    user: dict = Depends(get_current_user),
):
    """
    NOTE: this endpoint does NOT support the Idempotency-Key header that /upload
    accepts. No client currently calls it, so wiring it would be speculative — but
    do not assume a retry here is safe. Add the same guard before pointing a client
    at it.

    Upload a file by streaming the raw request body (Content-Type: application/octet-stream).

    Bypasses Starlette's multipart SpooledTemporaryFile buffering entirely — data flows
    directly from the TCP socket to the destination file with zero intermediate copies.
    Use this endpoint for large files (>1 GB) from the web portal.
    """
    if user.get("type") == "device":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Device tokens cannot upload files")

    user_record = await store.find_user(user.get("sub", ""))
    if user_record is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "User not found")
    safe_username = Path(user_record["name"]).name

    dest, upload_source, upload_source_folder = await _resolve_upload_destination(
        path, syncScope, sourceFolder, user, safe_username,
    )

    try:
        result = await ingest(
            request.stream(), filename=filename, dest=dest, user=user,
            original_date=original_date, source=upload_source, source_folder=upload_source_folder,
        )
    except IngestStallError:
        raise HTTPException(status.HTTP_408_REQUEST_TIMEOUT, "Upload stalled — no data received")
    except IngestSizeError as e:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, str(e))
    except IngestDiskFullError as e:
        raise HTTPException(status.HTTP_507_INSUFFICIENT_STORAGE, str(e))

    safe_name = result.path.name
    user_name = user.get("sub", "unknown")
    asyncio.create_task(_post_upload_notify(safe_name, user_name, str(result.path)))
    _invalidate_scan_cache(str(result.path.parent))

    if _is_document_file(result.path):
        from ..document_index import index_document
        asyncio.create_task(
            index_document(str(result.path), safe_name, user_name),
            name=f"index_upload_{safe_name}",
        )

    if result.path.suffix.lower() in _THUMB_VIDEO_EXTS:
        asyncio.create_task(
            _pregenerate_video_thumbnail(result.path),
            name=f"thumb_pregenerate_{safe_name}",
        )

    return {
        "name": safe_name,
        "path": "/" + str(result.path.relative_to(settings.nas_root.resolve())).replace("\\", "/"),
        "sizeBytes": result.bytes_written,
        "sortedTo": result.sorted_to,
    }


def _personal_owner_of(resolved_path: str) -> str | None:
    """The lowercased owner name if *resolved_path* lives under a /personal/<name>/ scope, else
    None (family/entertainment/other -- not owner-restricted). Same convention as
    backup_routes.py's _visible_duplicate_sets (M-2 fix, security audit 2026-08)."""
    parts = Path(resolved_path).parts
    for i, part in enumerate(parts):
        if part.lower() == "personal" and i + 1 < len(parts):
            return parts[i + 1].lower()
    return None


async def _post_upload_notify(safe_name: str, user_name: str, resolved_path: str) -> None:
    """Fire-and-forget: emit upload event + file event after response is sent."""
    try:
        await emit_upload_complete(safe_name, user_name, personal_owner=_personal_owner_of(resolved_path))
        await file_event_bus.publish(FileEvent(
            path=resolved_path,
            action="upload",
            user=user_name,
        ))
    except Exception as exc:
        # Best-effort notification — the upload itself already succeeded and its response was
        # already sent, so this must never raise. Logged (not just swallowed) so a systemic
        # notification failure is at least debuggable instead of invisible.
        logger.warning("post_upload_notify_failed file=%s user=%s error=%s", safe_name, user_name, exc)


@router.get(
    "/download",
    # Returns file bytes, not JSON. Without this the contract claims an untyped
    # JSON body and a generated client would try to deserialise a download.
    responses={200: {"content": {"application/octet-stream": {}}}},
    response_class=Response,
)
@limiter.limit("120/minute")
async def download_file(
    request: Request,
    path: str = Query(..., description="NAS path to the file to download"),
    user: dict = Depends(get_current_user),
):
    """
    Download or stream a file from the NAS.
    Supports HTTP Range requests (RFC 7233) for video/audio seeking and resumable downloads.
    Responds with 206 Partial Content for range requests, 200 OK for full file.
    """
    _require_external_storage()
    resolved = _safe_resolve(path)
    await _authorize_path(resolved, user)

    if not resolved.exists():
        from ..document_index import remove_document
        await remove_document(path)
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found")
    if resolved.is_dir():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Cannot download a directory")

    return stream_file_download(request, resolved)


def stream_file_download(request: Request, resolved: Path) -> Response:
    """Stream an already-resolved+authorized file, with HTTP Range support (RFC 7233) for
    video/audio seeking and resumable downloads. Responds with 206 Partial Content for range
    requests, 200 OK for full file.

    Shared by the main NAS /download route and local_backup_routes.py's read-only backup-drive
    /download route — both need identical Range/streaming behavior for an already-resolved
    Path, just a different (and differently-authorized) way of arriving at *resolved*.

    CALLER CONTRACT: *resolved* must already exist, be a file (not a directory), and be
    authorized for the current user before this is called.
    """
    mime, _ = mimetypes.guess_type(resolved.name)
    file_size = resolved.stat().st_size

    ascii_name = resolved.name.encode("ascii", errors="replace").decode("ascii")
    utf8_name = quote(resolved.name.encode("utf-8"), safe="")
    content_disposition = (
        'inline; filename="' + ascii_name + '"; filename*=UTF-8\'\'' + utf8_name
    )

    range_header = request.headers.get("Range")

    if range_header:
        try:
            if not range_header.startswith("bytes="):
                raise ValueError("unsupported range unit")
            # Take the first range spec only (multi-range not supported)
            range_spec = range_header[6:].split(",")[0].strip()
            start_str, _, end_str = range_spec.partition("-")
            start_str = start_str.strip()
            end_str = end_str.strip()

            if start_str == "" and end_str == "":
                raise ValueError("empty range spec")
            elif start_str == "":
                # suffix range: bytes=-N means last N bytes
                start = max(0, file_size - int(end_str))
                end = file_size - 1
            elif end_str == "":
                # open-ended: bytes=N- means from N to EOF
                start = int(start_str)
                end = file_size - 1
            else:
                start = int(start_str)
                end = int(end_str)

            if start < 0 or end >= file_size or start > end:
                raise ValueError("range out of bounds")

        except (ValueError, IndexError):
            return Response(
                status_code=416,
                headers={
                    "Content-Range": f"bytes */{file_size}",
                    "Accept-Ranges": "bytes",
                },
            )

        content_length = end - start + 1

        def _range_stream():
            _remaining = content_length
            with open(resolved, "rb") as fh:
                fh.seek(start)
                while _remaining > 0:
                    data = fh.read(min(262_144, _remaining))
                    if not data:
                        break
                    _remaining -= len(data)
                    yield data

        return StreamingResponse(
            _range_stream(),
            status_code=206,
            media_type=mime or "application/octet-stream",
            headers={
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Content-Length": str(content_length),
                "Content-Disposition": content_disposition,
                "Accept-Ranges": "bytes",
            },
        )

    # No Range header — stream the full file
    def _full_stream():
        with open(resolved, "rb") as fh:
            while True:
                data = fh.read(262_144)
                if not data:
                    break
                yield data

    return StreamingResponse(
        _full_stream(),
        media_type=mime or "application/octet-stream",
        headers={
            "Content-Length": str(file_size),
            "Content-Disposition": content_disposition,
            "Accept-Ranges": "bytes",
        },
    )


# Checked against the folder's total UNCOMPRESSED size before building the archive (cheap to
# compute, and most media in this app -- JPEG/H.264 -- barely compresses further anyway) so an
# oversized folder is rejected before spending time zipping it, not after. Paras's call,
# 2026-07-23: 200MB, with a clear client-side popup telling the user to pick a smaller bundle.
MAX_ZIP_SOURCE_BYTES = 200 * 1024 * 1024


def _build_zip(source: Path) -> Path:
    """Zips every file under `source` into a temp file, using paths relative to `source` as
    arcnames. Designed to run in a thread executor -- synchronous zipfile I/O would otherwise
    block the event loop. Caller owns deleting the returned temp file."""
    fd, tmp_name = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in source.rglob("*"):
                # Skip symlinks -- _safe_resolve already rejects one at the top-level requested
                # path, but a symlink planted deeper inside (out-of-band filesystem access, not
                # reachable through any app upload path today) would otherwise have its target's
                # content silently included even if that target is outside nas_root.
                if f.is_file() and not f.is_symlink():
                    zf.write(f, arcname=str(f.relative_to(source)))
    except Exception:
        # A failure partway through (file deleted mid-scan, permission error) never returns
        # tmp_path to the caller, so "caller owns deleting it" can't apply -- clean up here
        # instead of leaking the temp file permanently.
        tmp_path.unlink(missing_ok=True)
        raise
    return tmp_path


@router.get(
    "/download-zip",
    responses={200: {"content": {"application/zip": {}}}},    response_class=Response,
)
@limiter.limit("20/minute")
async def download_folder_zip(
    request: Request,
    path: str = Query(..., description="NAS path to the folder to download as a zip"),
    user: dict = Depends(get_current_user),
):
    """Download a folder as a .zip archive, capped at MAX_ZIP_SOURCE_BYTES of source content."""
    _require_external_storage()
    resolved = _safe_resolve(path)
    await _authorize_path(resolved, user)

    if not resolved.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Folder not found")
    if not resolved.is_dir():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Not a folder")

    # _authorize_path only restricts a path shaped exactly like /personal/<name>/... -- a bare
    # nas_root or nas_root/personal has too few path parts to trigger that check at all, so it
    # returns "no restriction" even though recursively zipping it (below) would walk straight
    # through every user's personal subtree. Block those two ancestors explicitly; anything at
    # or under a specific /personal/<name> is fine, since _authorize_path already enforced
    # ownership on that exact target above. Security review finding, 2026-07-23.
    nas_root = settings.nas_root.resolve()
    if resolved in (nas_root, nas_root / "personal"):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Select a specific folder to zip, not the whole library.",
        )

    loop = asyncio.get_running_loop()
    total_size = await loop.run_in_executor(None, _calc_dir_size_capped, resolved, MAX_ZIP_SOURCE_BYTES)
    if total_size > MAX_ZIP_SOURCE_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"This folder is {total_size / 1_048_576:.0f} MB, over the 200 MB zip limit. "
            "Select a subfolder or fewer files instead.",
        )

    tmp_path = await loop.run_in_executor(None, _build_zip, resolved)

    zip_name = resolved.name + ".zip"
    ascii_name = zip_name.encode("ascii", errors="replace").decode("ascii")
    utf8_name = quote(zip_name.encode("utf-8"), safe="")
    content_disposition = (
        'attachment; filename="' + ascii_name + '"; filename*=UTF-8\'\'' + utf8_name
    )

    def _stream_and_cleanup():
        try:
            with open(tmp_path, "rb") as fh:
                while chunk := fh.read(262_144):
                    yield chunk
        finally:
            tmp_path.unlink(missing_ok=True)

    return StreamingResponse(
        _stream_and_cleanup(),
        media_type="application/zip",
        headers={
            "Content-Length": str(tmp_path.stat().st_size),
            "Content-Disposition": content_disposition,
        },
    )


@router.get("/search", response_model=DocumentSearchResponse)
@limiter.limit("120/minute")
async def search_files(
    request: Request,
    q: str = Query(..., min_length=1, max_length=200, description="Full-text search query"),
    limit: int = Query(10, ge=1, le=50),
    user: dict = Depends(get_current_user),
):
    """
    Full-text search over indexed documents in the NAS.
    Admins see all results; regular users see only their own and shared documents.
    """
    from ..document_index import search_documents

    # The JWT carries only the user id (sub) — resolve the real personal-folder
    # name and admin flag from the store, else members match nothing of their own
    # and admins are wrongly treated as members.
    name, is_admin = await _resolve_identity(user)
    user_role = "admin" if is_admin else "member"
    results = await search_documents(query=q, limit=limit, user_role=user_role, username=name)
    return {"results": results, "query": q, "count": len(results)}


def _fuse_by_rank(text_hits, image_hits, *, limit: int, k: int = 60):
    """
    Merge two rankings without ever comparing their scores.

    The two models live in unrelated spaces and their similarities are not on the same scale —
    measured here, CLIP cosines sit around 0.1–0.3 while bge sits at 0.4–0.7. Sorting a merged list
    by raw score would therefore bury every image match beneath every text match, regardless of how
    good it was, and would look like "image search does not work" rather than a units bug.

    Reciprocal Rank Fusion uses only *positions*: each list contributes `1 / (k + rank)`, and a file
    found by both rises above one found by either. `k = 60` is the value from the original paper and
    needs no tuning — it simply damps how much the very top of one list can dominate.

    The returned score is a fusion score, not a similarity, and is only meaningful as an ordering.
    """
    scores: dict[str, float] = {}
    for hits in (text_hits, image_hits):
        for rank, hit in enumerate(hits, start=1):
            scores[hit.rel_path] = scores.get(hit.rel_path, 0.0) + 1.0 / (k + rank)
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    return ranked[:limit]


@router.get("/search/semantic", response_model=SemanticSearchResponse)
@limiter.limit("60/minute")
async def search_files_semantic(
    request: Request,
    q: str = Query(..., min_length=1, max_length=200, description="Natural-language query"),
    limit: int = Query(20, ge=1, le=50),
    user: dict = Depends(get_current_user),
):
    """
    Meaning-based search over the indexed library, as opposed to `/search`'s keyword matching.

    Returns **501** when this board cannot do it — the embedding runtime is an optional ~200 MB
    dependency and the model files are downloaded separately, so a perfectly healthy board may
    simply not have them. That is not an error state to retry; it is a capability the client
    should stop offering, which is why it mirrors the 501 the host-capability guards return
    rather than a 503.

    Rate limited lower than `/search` because each call runs a model: measured ~23 ms of CPU per
    query on ARM, against a keyword lookup's microseconds.
    """
    from .. import embedding, image_embedding, vector_store

    if not embedding.available():
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Semantic search is not installed on this device",
        )

    provider = embedding.OnnxEmbeddingProvider()
    query_vector = await run_in_threadpool(provider.embed_query, q)

    # Own shard plus shared — never another member's personal library. This is the privacy
    # boundary as much as the performance one: measured on the production board, two 40k shards
    # cost 11 ms each where one combined 200k index costs 65 ms against a ~52 ms budget.
    name, _is_admin = await _resolve_identity(user)
    shards = vector_store.shards_for_user(name)
    text_hits = await run_in_threadpool(
        lambda: vector_store.search_shards(
            query_vector, model=provider.model_name, shards=shards, limit=limit
        )
    )

    # Image vectors, when this board has them. A separate model in a separate space, searched over
    # the same shards — so the privacy boundary is identical and nothing extra had to be built.
    image_hits: list = []
    models = [provider.model_name]
    if image_embedding.available():
        try:
            img = image_embedding.ImageEmbeddingProvider()
            img_query = await run_in_threadpool(img.embed_query, q)
            image_hits = await run_in_threadpool(
                lambda: vector_store.search_shards(
                    img_query, model=img.model_name, shards=shards, limit=limit
                )
            )
            models.append(img.model_name)
        except Exception as exc:  # image search is an enhancement, never a dependency
            logger.warning("image_search_failed error=%s", exc)

    fused = _fuse_by_rank(text_hits, image_hits, limit=limit)
    return {
        "results": [
            {"path": path, "filename": path.rsplit("/", 1)[-1], "score": score}
            for path, score in fused
        ],
        "query": q,
        "count": len(fused),
        "model": "+".join(models),
    }


@router.get("/search/semantic/status", response_model=SemanticIndexStatusResponse)
async def semantic_index_status(request: Request, user: dict = Depends(get_current_user)):
    """
    Whether semantic search works here, and how far the index has got.

    Deliberately answers even when the feature is unavailable — that is the case a client most
    needs to distinguish, since "no results" and "not installed" look identical otherwise.
    """
    from .. import embedding, job_store, semantic_indexer, vector_store

    spec = embedding.spec_for()
    job_id = semantic_indexer.current_job_id()
    progress = None
    if job_id:
        job = job_store.get_job(job_id)
        progress = job.progress if job else None

    try:
        name, _ = await _resolve_identity(user)
        indexed = sum(
            vector_store.index_count(spec.name, shard=sh)
            for sh in vector_store.shards_for_user(name)
        )
    except Exception:
        # A board that has never indexed has no shard files yet; that is zero, not an error.
        indexed = 0

    return {
        "available": embedding.available(spec=spec),
        "model": spec.name,
        "running": semantic_indexer.is_running(),
        "jobId": job_id,
        "indexedCount": indexed,
        "progress": progress,
    }


@router.post("/sort-now")
@limiter.limit("10/minute")
async def sort_now(
    request: Request,
    path: str = Query(..., description="NAS directory path to sort immediately"),
    user: dict = Depends(get_current_user),
):
    """
    Manually sort an existing folder into Photos/Videos/Documents/Others.
    Useful for bulk imports copied directly onto NAS (outside .inbox).
    """
    _require_external_storage()
    resolved = _safe_resolve(path)
    await _authorize_path(resolved, user)
    if not resolved.exists() or not resolved.is_dir():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Directory not found")

    from ..file_sorter import sort_folder_now, RESERVED_CATEGORY_NAMES

    if resolved.name in RESERVED_CATEGORY_NAMES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"This folder is already sorted content (\"{resolved.name}\") -- "
            "run Sort from its parent folder instead, not from inside it.",
        )

    added_by = user.get("sub", "unknown")
    stats = await sort_folder_now(resolved, added_by=added_by)
    nas_path = "/" + str(resolved.relative_to(settings.nas_root.resolve())).replace("\\", "/")
    return {
        "path": nas_path,
        **stats,
    }


@router.get("/roots", response_model=StorageRootsResponse)
async def storage_roots(user: dict = Depends(get_current_user)):
    """Return browseable storage roots — mounted USB/NVMe drives."""
    from .storage_helpers import build_device_list, list_block_devices

    raw = await list_block_devices()
    devices = build_device_list(raw)
    roots = []
    for dev in devices:
        if dev.mounted and dev.mount_point:
            roots.append({
                "name": dev.label or dev.model or dev.name,
                "path": dev.mount_point,
                "device": dev.path,
                "transport": dev.transport,
                "sizeBytes": dev.size_bytes,
                "sizeDisplay": dev.size_display,
                "fstype": dev.fstype or "",
                "label": dev.label or "",
                "model": dev.model or "",
            })
    return {"roots": roots}


def _thumb_cache_path(resolved: Path, mtime: float, size: int) -> Path:
    """Return the on-disk cache path for a thumbnail.

    SECURITY INVARIANT: this cache is a flat global, keyed purely on file identity (resolved
    path + mtime + size) — nothing about scope or the requesting user. It is NOT itself an
    authorization boundary. Every current caller of _thumbnail_response (below) authorizes the
    request via _authorize_path or _authorize_media_scope BEFORE ever reaching this cache — that
    ordering is what keeps this safe, not anything in the cache key itself. If a future route
    ever serves a _thumb_cache_path() result (or calls _thumbnail_response) without an auth
    check strictly before it, that new route would leak private thumbnails cross-user, since a
    cache hit here skips regeneration and skips whatever authorization the *original* writer had
    to pass — reviewed 2026-07-10 (audit M7), keep this comment in sync with any new caller."""
    key = hashlib.sha256(f"{resolved}\x00{mtime}\x00{size}".encode()).hexdigest()
    return settings.data_dir / "thumb_cache" / f"{key}.jpg"


async def _generate_image_thumbnail(resolved: Path, size: int) -> bytes:
    """Generate a JPEG thumbnail from an image using Pillow.

    img.draft() must be called before anything that forces a decode (exif_transpose/thumbnail/
    save all do). For JPEG it lets libjpeg downscale during the DCT decode step itself instead
    of decoding at full resolution and shrinking afterward — for a 12MP source down to a 256px
    thumbnail this is roughly a 70-100x reduction in decode working-set (~0.5MB vs ~36MB+).
    Non-JPEG formats silently no-op here and still pay full decode cost, which is why this path
    is also concurrency-capped below.
    """
    def _make() -> bytes:
        with Image.open(resolved) as img:
            img.draft("RGB", (size, size))
            img = ImageOps.exif_transpose(img)
            img.thumbnail((size, size), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=80, optimize=True)
            return buf.getvalue()

    loop = asyncio.get_running_loop()
    async with _THUMB_IMAGE_SEMAPHORE:
        return await loop.run_in_executor(None, _make)


async def _generate_video_thumbnail(resolved: Path, size: int) -> bytes:
    """Generate a JPEG thumbnail by grabbing a single frame with ffmpeg."""
    def _grab(seek: str) -> bytes:
        proc = subprocess.run(
            [
                _THUMB_FFMPEG,
                "-threads", "1",  # don't let ffmpeg add its own internal parallelism on top of ours
                "-ss", seek,
                "-i", str(resolved),
                "-frames:v", "1",
                "-vf", f"scale='min({size},iw)':-2",
                "-f", "mjpeg",
                "-",
            ],
            capture_output=True,
            timeout=15,
        )
        if proc.returncode != 0 or not proc.stdout:
            return b""
        return proc.stdout

    def _make() -> bytes:
        # Short clips or odd keyframe layouts yield nothing at 1s — fall back to frame 0.
        for seek in ("1", "0"):
            out = _grab(seek)
            if out:
                return out
        raise RuntimeError("ffmpeg thumbnail failed")

    loop = asyncio.get_running_loop()
    async with _THUMB_VIDEO_SEMAPHORE:
        return await loop.run_in_executor(None, _make)


async def _thumbnail_response(resolved: Path, size: int, log_ref: str) -> Response:
    """Core thumbnail generation/caching, shared by the path-based /thumbnail
    route and the entry-id-based /media/{id}/thumbnail route (media_routes.py)
    — both need identical caching/generation behavior, just a different way
    of arriving at *resolved*. *log_ref* is only used in error logging.

    CALLER CONTRACT: *resolved* must already be authorized for the current user before this is
    called — see _thumb_cache_path's docstring for why. Both existing callers do this
    (_authorize_path in /thumbnail, _authorize_media_scope in /media/{id}/thumbnail); any new
    caller must do the same, in that order, before reaching this function."""
    if not resolved.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found")
    if resolved.is_dir():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Cannot thumbnail a directory")

    size = max(64, min(512, size))
    suffix = resolved.suffix.lower()
    src_stat = resolved.stat()
    cache_path = _thumb_cache_path(resolved, src_stat.st_mtime, size)

    try:
        if cache_path.exists() and cache_path.stat().st_mtime >= src_stat.st_mtime:
            data = cache_path.read_bytes()
            return Response(
                data,
                media_type="image/jpeg",
                headers={"Cache-Control": f"private, max-age={_THUMB_MAX_AGE}"},
            )
    except Exception:
        pass

    try:
        if suffix in _THUMB_IMAGE_EXTS:
            data = await _generate_image_thumbnail(resolved, size)
        elif suffix in _THUMB_VIDEO_EXTS:
            data = await _generate_video_thumbnail(resolved, size)
        else:
            raise HTTPException(
                status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                "Unsupported file type for thumbnail",
            )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Thumbnail generation failed for %s: %s", log_ref, exc)
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Thumbnail not available")

    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(cache_path)
    except Exception as exc:
        logger.error("Failed to write thumbnail cache %s: %s", cache_path, exc)

    return Response(
        data,
        media_type="image/jpeg",
        headers={"Cache-Control": f"private, max-age={_THUMB_MAX_AGE}"},
    )


async def _pregenerate_video_thumbnail(resolved: Path) -> None:
    """Best-effort: generate + cache a video's thumbnail immediately after upload, at the same
    default size (256) every client requests, instead of waiting for the first viewer to trigger
    it on demand -- otherwise the Gallery grid shows a bare play-icon placeholder for a freshly
    uploaded video until someone happens to scroll to it, which reads as the upload having
    silently stalled. Fire-and-forget from the upload route (same pattern as
    _post_upload_notify/index_document below) -- must never raise into the upload response, and
    reuses _generate_video_thumbnail's own _THUMB_VIDEO_SEMAPHORE, so a burst of video uploads
    still can't exceed the RAM-safety concurrency cap that semaphore already enforces."""
    try:
        if not resolved.exists():
            return
        size = 256
        src_stat = resolved.stat()
        cache_path = _thumb_cache_path(resolved, src_stat.st_mtime, size)
        if cache_path.exists() and cache_path.stat().st_mtime >= src_stat.st_mtime:
            return
        data = await _generate_video_thumbnail(resolved, size)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(cache_path)
    except Exception as exc:
        logger.warning("thumbnail_pregenerate_failed path=%s error=%s", resolved, exc)


@router.get(
    "/thumbnail",
    responses={200: {"content": {"image/jpeg": {}}}},    response_class=Response,
)
@limiter.limit("120/minute")
async def thumbnail(
    request: Request,
    path: str = Query(..., description="NAS path to the image or video"),
    size: int = Query(256, description="Max thumbnail edge in pixels"),
    user: dict = Depends(get_current_user),
):
    """Return a small cached JPEG thumbnail for an image or video file."""
    _require_external_storage()
    resolved = _safe_resolve(path)
    await _authorize_path(resolved, user)
    return await _thumbnail_response(resolved, size, log_ref=path)


# Job IDs requested to cancel — the reindex loop checks this cooperatively between files.
_reindex_cancels: set[str] = set()


@router.post("/reindex", status_code=status.HTTP_202_ACCEPTED, response_model=ReindexStartedResponse)
@limiter.limit("6/hour")
async def reindex_storage(request: Request, user: dict = Depends(require_admin)):
    """Admin: re-scan NAS storage and rebuild both indexes to match the filesystem.

    The filesystem is the source of truth — this prunes index rows whose files are gone, then
    (re)indexes every supported file under the watched roots: documents + OCR text for images
    (search), and separately media_index (Photos/Videos/Documents/Others across every scope —
    the Folders/timeline browse index, plus the bucket-allocator counters it depends on). Runs
    in the background; poll `GET /api/v1/jobs/{jobId}` for status (`running` ->
    `completed`/`failed`, with `result={indexed, pruned, mediaIndexed, mediaPruned,
    bucketsRepaired}`).
    """
    job = create_job(user_id=user.get("sub", ""))

    async def _run() -> None:
        try:
            update_job(job.id, status=JobStatus.running, progress={"current": 0, "total": 0})
            from pathlib import Path as _Path
            from ..document_index import remove_missing_documents, nas_paths_with_ocr, _to_nas_path
            from ..index_watcher import (
                sync_once, _load_persisted_state, _save_persisted_state, _scan_documents_sync,
            )
            from ..media_reconciler import reconcile_once as _reconcile_media
            pruned = await remove_missing_documents()

            def _progress(current: int, total: int) -> None:
                update_job(job.id, progress={"current": current, "total": total})

            # Re-OCR files that are NEW or indexed with EMPTY text (e.g. a prior OCR timeout), while
            # skipping files that already have good OCR — so re-scan actually FIXES gaps efficiently.
            loop = asyncio.get_running_loop()
            well = await nas_paths_with_ocr()
            current_files = await loop.run_in_executor(None, _scan_documents_sync)
            redo = {ap for ap in current_files if _to_nas_path(_Path(ap)) not in well}

            new_state = await sync_once(
                _load_persisted_state(),
                on_progress=_progress,
                should_cancel=lambda: job.id in _reindex_cancels,
                redo_paths=redo,
            )
            if job.id in _reindex_cancels:
                _reindex_cancels.discard(job.id)
                # Don't persist partial state — leave un-indexed files "new" for the next scan.
                update_job(job.id, status=JobStatus.failed, error="cancelled")
                return
            _save_persisted_state(new_state)

            # Separate index, separate scan of the SAME tree — never allowed to undo the
            # document-index result above even if it fails.
            media_result = {"pruned": 0, "indexed": 0, "buckets_repaired": 0}
            try:
                media_result = await _reconcile_media()
            except Exception as exc:
                logger.error("reindex_media_reconcile_failed error=%s", exc)

            # Semantic embedding runs *behind* this, as its own job. The metadata scan is what
            # makes files appear in the app and must finish now; embedding the same library costs
            # tens of minutes to hours, so joining them would mean the app waits on inference.
            # A board without the runtime returns no job and this is a no-op.
            semantic_job_id = None
            try:
                from .. import semantic_indexer
                from ..config import settings as _settings
                # Diffed in SQL against media.db rather than by handing over the whole library as
                # a dict — at 10 lakh items that dict is ~300 MB of Python strings, more than the
                # smallest board has free.
                semantic_job_id, _ = semantic_indexer.request_from_db(
                    _settings.data_dir / "media.db", user_id=user.get("sub", "")
                )
            except Exception as exc:  # never let semantic search fail a metadata reindex
                logger.warning("semantic_index_request_failed error=%s", exc)

            update_job(
                job.id,
                status=JobStatus.completed,
                result={
                    "indexed": len(new_state),
                    "pruned": pruned,
                    "mediaIndexed": media_result["indexed"],
                    "mediaPruned": media_result["pruned"],
                    "bucketsRepaired": media_result["buckets_repaired"],
                    "semanticJobId": semantic_job_id,
                },
            )
        except Exception as exc:  # surface any failure as a failed job, never crash the worker
            _reindex_cancels.discard(job.id)
            logger.error("reindex_failed error=%s", exc)
            update_job(job.id, status=JobStatus.failed, error=str(exc))

    asyncio.create_task(_run())
    return {"jobId": job.id, "status": "running"}


@router.post("/reindex/cancel", response_model=ReindexCancelledResponse)
async def cancel_reindex(request: Request, jobId: str = Query(...), user: dict = Depends(require_admin)):
    """Admin: request cancellation of a running re-scan (stops after the current batch of files)."""
    _reindex_cancels.add(jobId)
    return {"cancelled": jobId}
