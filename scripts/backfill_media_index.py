#!/usr/bin/env python3
"""
One-shot / re-runnable backfill: walk existing on-disk NAS data and record it
into media.db. Needed because files ingested before media_index.py existed
(and anything ever touched directly over the raw SMB share) have no DB row —
without this, the app's media-query endpoints show nothing for them. Read-
only over the filesystem: never moves, renames, or deletes a file, only
inserts/updates media.db rows. Safe to re-run any time — media.db's
blobs.rel_path UNIQUE constraint makes every insert an idempotent upsert.

Usage:
    python -m scripts.backfill_media_index
"""

import asyncio
import hashlib
import mimetypes
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("AHC_SKIP_MOUNT_CHECK", "true")
os.environ.setdefault("AHC_NAS_ROOT", "/srv/nas")
os.environ.setdefault("AHC_DATA_DIR", "/var/lib/aihomecloud")

_SKIP_SUFFIXES = (".uploading", ".part", ".tmp")
_SKIP_DIR_NAMES = {".inbox", ".ahc_trash", ".avatars", "lost+found"}
_KNOWN_CATEGORY_DIRS = {"Photos", "Videos", "Documents", "Others", "Movies", "Series", "Music"}


def _sha256_of(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _collect_scope_roots(settings):
    roots = [
        ("family", None, settings.family_path),
        ("entertainment", None, settings.entertainment_path),
    ]
    if settings.personal_path.is_dir():
        for user_dir in sorted(settings.personal_path.iterdir()):
            if user_dir.is_dir():
                roots.append(("personal", user_dir.name, user_dir))
    return roots


async def main() -> None:
    from app import media_index
    from app.config import settings
    from app.file_sorter import _destination_folder
    from app.ingest import _extract_video_duration, _resolve_capture_epoch

    print(f"NAS root: {settings.nas_root}")
    await media_index.init_db()

    scanned = 0
    recorded = 0
    skipped = 0
    nas_root_resolved = settings.nas_root.resolve()

    for scope, owner, scope_root in _collect_scope_roots(settings):
        if not scope_root.is_dir():
            continue
        print(f"Scanning {scope}/{owner or ''} at {scope_root} ...")

        for file_path in scope_root.rglob("*"):
            if not file_path.is_file():
                continue
            rel_parts = file_path.relative_to(scope_root).parts
            if any(part in _SKIP_DIR_NAMES for part in rel_parts):
                skipped += 1
                continue
            if file_path.name.endswith(_SKIP_SUFFIXES):
                skipped += 1
                continue

            scanned += 1
            try:
                category = next((p for p in rel_parts if p in _KNOWN_CATEGORY_DIRS), None)
                if category is None:
                    # Not physically inside a category folder (e.g. Sync's
                    # Backups/<name>/ raw dump) — derive a virtual category
                    # the same way live MIRROR-mode ingest does.
                    base_dir = scope_root if scope == "entertainment" else None
                    category = _destination_folder(file_path, base_dir=base_dir)

                source, source_folder = "direct_upload", None
                if "Backups" in rel_parts:
                    idx = rel_parts.index("Backups")
                    if idx + 1 < len(rel_parts):
                        source, source_folder = "sync", rel_parts[idx + 1]

                stat = file_path.stat()
                capture_epoch = _resolve_capture_epoch(
                    file_path, file_path.name, fallback_epoch=stat.st_mtime,
                )
                sha256 = _sha256_of(file_path)
                rel_path = "/" + str(file_path.resolve().relative_to(nas_root_resolved))
                media_type, _ = mimetypes.guess_type(file_path.name)
                duration = _extract_video_duration(file_path)

                await media_index.record_entry(
                    rel_path=rel_path,
                    content_hash=sha256,
                    size_bytes=stat.st_size,
                    scope=scope,
                    owner=owner,
                    category=category,
                    media_type=media_type,
                    source=source,
                    source_folder=source_folder,
                    filename=file_path.name,
                    original_name=file_path.name,
                    capture_date=capture_epoch,
                    duration=duration,
                )
                recorded += 1
                if recorded % 200 == 0:
                    print(f"  ... {recorded} recorded ({scanned} scanned so far)")
            except Exception as exc:
                print(f"  WARN: failed to index {file_path}: {exc}")
                skipped += 1

    print()
    print("Reconciling: marking entries deleted whose file no longer exists on disk "
          "(covers direct-SMB deletes/moves, and /files/delete's soft-delete-to-trash, "
          "which doesn't yet update media_index) ...")
    marked_deleted = await media_index.mark_missing_deleted(nas_root_resolved)

    await media_index.close_db()
    print()
    print(f"Scanned: {scanned}  Recorded: {recorded}  Skipped: {skipped}  Marked deleted: {marked_deleted}")


if __name__ == "__main__":
    asyncio.run(main())
