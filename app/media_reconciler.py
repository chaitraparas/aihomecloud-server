"""
Nightly reconciler for media_index — keeps the logical browse index in sync
with whatever is actually on disk, independent of ingest().

Why this exists: media_index is only ever written at ingest time (see its
own module docstring — "a queryable cache alongside the filesystem, not the
source of truth"). Two things can drift it from reality with nothing to
notice: a file dropped in over the raw SMB share outside the app entirely,
and the bucket allocator's counters (media_index.buckets) silently
disagreeing with a real directory scan after a crash between allocation and
write. This module is the backstop for both — reusing the exact incremental-
skip pattern already proven in index_watcher.py (the document/OCR watcher),
generalized to the full media tree (Photos/Videos/Documents/Others across
every scope, not just Documents/).

Deliberately NOT a full nightly rehash: SHA-256 over a whole family photo
library is hours of CPU+I/O on a 1GB-RAM ARM board, not viable to run every
night. Only files whose (rel_path, size_bytes, mtime) differ from what's
already recorded get re-hashed — see media_index.existing_blob_signatures().
"""

import asyncio
import hashlib
import logging
import mimetypes
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Callable, Optional

from . import media_index
from .config import settings
from . import workload
from .file_sorter import _destination_folder
from .ingest import _extract_video_duration, _resolve_capture_epoch

logger = logging.getLogger("aihomecloud.media_reconciler")

_SKIP_SUFFIXES = (".uploading", ".part", ".tmp")
_SKIP_DIR_NAMES = {".inbox", ".ahc_trash", ".avatars", "lost+found"}
_KNOWN_CATEGORY_DIRS = {"Photos", "Videos", "Documents", "Others", "Movies", "Series", "Music"}

# Run once per day at this local hour — low-traffic overnight window, same
# rationale as the board's other maintenance windows.
_RECONCILE_HOUR = 3


def _iter_media_roots() -> list[tuple[str, Optional[str], Path]]:
    """(scope, owner, root) for every tree the reconciler walks — mirrors
    scripts/backfill_media_index.py's _collect_scope_roots exactly, since this
    module is that script's logic made incremental and runnable in-process."""
    roots: list[tuple[str, Optional[str], Path]] = [
        ("family", None, settings.family_path),
        ("entertainment", None, settings.entertainment_path),
    ]
    if settings.personal_path.is_dir():
        for user_dir in sorted(settings.personal_path.iterdir()):
            if user_dir.is_dir():
                roots.append(("personal", user_dir.name, user_dir))
    return roots


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _scan_current_files_sync() -> dict[str, tuple[Path, str, Optional[str], int, int]]:
    """rel_path -> (abs_path, scope, owner, size_bytes, mtime_int) for every real
    media file on disk right now. Uses rglob's lazy generator (not materialized
    into a list) — the streaming contract this module's docstring commits to."""
    nas_root_resolved = settings.nas_root.resolve()
    current: dict[str, tuple[Path, str, Optional[str], int, int]] = {}

    for scope, owner, scope_root in _iter_media_roots():
        if not scope_root.is_dir():
            continue
        for file_path in scope_root.rglob("*"):
            if not file_path.is_file():
                continue
            try:
                rel_parts = file_path.relative_to(scope_root).parts
            except ValueError:
                continue
            if any(part in _SKIP_DIR_NAMES for part in rel_parts):
                continue
            if file_path.name.endswith(_SKIP_SUFFIXES):
                continue
            try:
                stat = file_path.stat()
                rel_path = "/" + str(file_path.resolve().relative_to(nas_root_resolved))
                current[rel_path] = (file_path, scope, owner, stat.st_size, int(stat.st_mtime))
            except OSError:
                continue

    return current


def _infer_legacy_source(rel_parts: tuple[str, ...]) -> tuple[str, Optional[str]]:
    """Best-effort (source, source_folder) for a file discovered by the reconciler
    that ingest() never recorded — e.g. a pre-migration file still physically
    sitting in a legacy Backups/<folder-id>/ location (the physical sync tree
    retired by the SORTED-path unification, but old on-disk data may still be
    there until an explicit cleanup runs), or anything dropped in over SMB."""
    if "Backups" in rel_parts:
        idx = rel_parts.index("Backups")
        if idx + 1 < len(rel_parts):
            return "sync", rel_parts[idx + 1]
    return "direct_upload", None


async def reconcile_once(
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> dict[str, int]:
    """One full reconcile pass: prune entries for files no longer on disk, index
    new/changed files (skipping unchanged ones), repair bucket-allocator counters.
    Safe to call concurrently with live traffic and to re-run any time — every
    step here is idempotent. Returns {"pruned", "scanned", "indexed", "skipped",
    "buckets_repaired"}."""
    nas_root_resolved = settings.nas_root.resolve()

    pruned = await media_index.mark_missing_deleted(nas_root_resolved)

    loop = asyncio.get_running_loop()
    current = await loop.run_in_executor(None, _scan_current_files_sync)
    existing_sigs = await media_index.existing_blob_signatures()

    to_index = [
        rel_path for rel_path, (_, _, _, size, mtime) in current.items()
        if existing_sigs.get(rel_path) != (size, mtime)
    ]
    total = len(to_index)
    scanned = len(current)
    indexed = 0
    if on_progress is not None:
        on_progress(0, total)

    for i, rel_path in enumerate(to_index, start=1):
        file_path, scope, owner, size_bytes, mtime = current[rel_path]
        try:
            # rel_path is nas-root-relative ("/personal/alice/Photos/2025/07/x.jpg" or
            # "/family/Backups/<id>/x.jpg") — parts work identically for category/
            # legacy-source detection whether split from the scope root or nas root,
            # since both checks just search for a matching name anywhere in the path.
            rel_parts = tuple(p for p in rel_path.split("/") if p)
            category = next((p for p in rel_parts if p in _KNOWN_CATEGORY_DIRS), None)
            if category is None:
                base_dir = (
                    settings.entertainment_path if scope == "entertainment" else None
                )
                category = _destination_folder(file_path, base_dir=base_dir)

            source, source_folder = _infer_legacy_source(rel_parts)
            capture_epoch = _resolve_capture_epoch(
                file_path, file_path.name, fallback_epoch=mtime,
            )
            sha256 = await loop.run_in_executor(None, _sha256_of, file_path)
            media_type, _ = mimetypes.guess_type(file_path.name)
            video_duration = _extract_video_duration(file_path)

            await media_index.record_entry(
                rel_path=rel_path,
                content_hash=sha256,
                size_bytes=size_bytes,
                scope=scope,
                owner=owner,
                category=category,
                media_type=media_type,
                source=source,
                source_folder=source_folder,
                filename=file_path.name,
                original_name=file_path.name,
                capture_date=capture_epoch,
                mtime=float(mtime),
                duration=video_duration,
            )
            indexed += 1
        except Exception as exc:
            logger.warning("reconcile_index_failed rel_path=%s error=%s", rel_path, exc)
        if on_progress is not None:
            on_progress(i, total)

    buckets_repaired = await media_index.repair_bucket_counts(nas_root_resolved)

    if pruned or indexed or buckets_repaired:
        logger.info(
            "media_reconcile pruned=%d scanned=%d indexed=%d buckets_repaired=%d",
            pruned, scanned, indexed, buckets_repaired,
        )

    return {
        "pruned": pruned,
        "scanned": scanned,
        "indexed": indexed,
        "skipped": scanned - len(to_index),
        "buckets_repaired": buckets_repaired,
    }


def _seconds_until_next_run(hour: int = _RECONCILE_HOUR) -> float:
    """Seconds from now until the next occurrence of `hour`:00 local time —
    today if that hour hasn't passed yet, otherwise tomorrow."""
    now = datetime.now()
    target = datetime.combine(now.date(), dtime(hour=hour))
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


class MediaReconciler:
    """Background task that runs reconcile_once() once nightly (~03:00 local)."""

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="media_reconciler")
        logger.info("MediaReconciler started — next run in %.0fs", _seconds_until_next_run())

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        logger.info("MediaReconciler stopped")

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(_seconds_until_next_run())
            # A full library hash sweep is exactly what must not run while someone is
            # uploading or streaming. It has waited hours already; it can wait for quiet.
            await workload.gate("media_reconcile", max_wait=24 * 60 * 60)
            try:
                await reconcile_once()
            except Exception as exc:
                logger.error("MediaReconciler pass error: %s", exc)


_reconciler = MediaReconciler()


def get_media_reconciler() -> MediaReconciler:
    return _reconciler
