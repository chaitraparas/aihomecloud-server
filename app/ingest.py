"""
Unified file-ingestion core.

Every path that writes bytes into the NAS (direct app upload, phone auto-sync,
Telegram bot, web upload portal) funnels through :func:`ingest` (or, for callers
that need custom duplicate-handling UX like the Telegram bot, the lower-level
:func:`stream_to_temp`). This replaces four independent, drifting
implementations of "write a file to a bucket" with one hardened writer.

Also hosts the path-safety/authorization helpers (`_safe_resolve`,
`_authorize_path`, `_require_external_storage`) and capture-time helpers
(`_apply_capture_time`) that used to live in `routes/file_routes.py` — moved
here so both `file_routes.py` and this module can share them without a
circular import. `file_routes.py` re-exports them for backward compatibility
with existing external importers (`trash_routes.py`, `web_browser_routes.py`,
`auth_routes.py`, `telegram_upload_routes.py`).
"""

import asyncio
import errno
import hashlib
import logging
import mimetypes
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import AsyncIterator, Optional

from fastapi import HTTPException, status

from . import store
from . import media_index
from .config import settings
from .file_sorter import _destination_folder, _unique_dest, _sort_file, _date_bucket_dir

logger = logging.getLogger("aihomecloud.ingest")

# Larger write buffer for uploads — 2 MB (fewer syscalls on ARM).
_UPLOAD_WRITE_BUF = 2 * 1024 * 1024

BLOCKED_EXTENSIONS: frozenset[str] = frozenset({
    ".sh", ".bash", ".zsh", ".fish",
    ".py", ".rb", ".pl", ".php",
    ".elf", ".bin", ".exe",
    ".apk", ".so", ".ko",
    ".deb", ".rpm",
})


# ---------------------------------------------------------------------------
# Path safety / authorization (moved from file_routes.py, unchanged logic)
# ---------------------------------------------------------------------------

def _require_external_storage() -> None:
    """Verify that external storage (USB / NVMe) is mounted at nas_root.

    If nas_root is just a directory on the SD card, reject file operations so
    users don't accidentally browse OS files.
    """
    if settings.skip_mount_check:
        return
    nas = settings.nas_root
    if not nas.is_mount():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "No external storage mounted. Please connect a USB or NVMe drive.",
        )


def _safe_resolve(raw_path: str) -> Path:
    """Resolve a NAS-relative path to an absolute filesystem path, ensuring it
    stays within nas_root."""
    if "\x00" in raw_path:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Path outside NAS root")
    if len(raw_path) > 4096:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Path too long")

    nas_prefix = str(settings.nas_root)
    if raw_path.startswith(nas_prefix):
        boundary = len(nas_prefix)
        if len(raw_path) == boundary or raw_path[boundary] in ("/", "\\"):
            raw_path = raw_path[boundary:]

    candidate = settings.nas_root / raw_path.lstrip("/")

    if candidate.is_symlink():
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Symbolic links are not allowed")

    try:
        resolved = candidate.resolve()
    except (OSError, ValueError):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Path outside NAS root")

    nas_resolved = settings.nas_root.resolve()
    try:
        resolved.relative_to(nas_resolved)
    except ValueError:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Path outside NAS root")
    return resolved


async def _resolve_identity(user: dict) -> tuple[str, bool]:
    """Resolve the authenticated principal to (personal_folder_name, is_admin)."""
    if user.get("type") == "device":
        return "", True
    found = await store.find_user(user.get("sub", ""))
    if not found:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User no longer exists")
    return found.get("name", ""), bool(found.get("is_admin", False))


async def _authorize_path(resolved: Path, user: dict) -> None:
    """Enforce that ``/personal/<name>/`` subtrees are private to their owner.

    The NAS root and the bare ``/personal`` directory are never "shared" — they're the
    *ancestors* of every user's private subtree, not a shared location themselves. Treating
    them as unrestricted (the old `len(rel_parts) < 2` early-return covered both) let any
    non-admin target `path=personal` or `path=` directly on a whole-tree operation like
    delete/rename and reach every family member's private files in one call, with no
    per-owner check ever running — found live 2026-07-30 via Stage 2's file-access council
    review (DeepSeek), confirmed reachable through DELETE /files/delete?path=personal.
    """
    rel_parts = resolved.relative_to(settings.nas_root.resolve()).parts
    if len(rel_parts) == 0 or (len(rel_parts) == 1 and rel_parts[0] == "personal"):
        _, is_admin = await _resolve_identity(user)
        if is_admin:
            return
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Operating on this path directly is not allowed",
        )
    if len(rel_parts) < 2 or rel_parts[0] != "personal":
        return  # shared location — no per-user restriction
    owner = rel_parts[1]
    name, is_admin = await _resolve_identity(user)
    if is_admin or Path(name).name == owner:
        return
    raise HTTPException(
        status.HTTP_403_FORBIDDEN,
        "Access to another user's personal files is not allowed",
    )


# ---------------------------------------------------------------------------
# Capture-time helpers (moved from file_routes.py, unchanged logic)
# ---------------------------------------------------------------------------

def _extract_capture_epoch(file_path: Path, suffix_hint: Optional[str] = None):
    """Return the media's embedded capture time (epoch seconds), or None.

    *suffix_hint* overrides file_path.suffix when the physical file's own name
    doesn't reflect its real extension (e.g. a "<name>.jpg.uploading" temp file
    mid-ingest, read before the final rename so a date-bucketed destination
    can be chosen). PIL/ffprobe both sniff actual file contents — only the
    *dispatch* decision (image vs. video strategy) needs the real extension."""
    suffix = (suffix_hint or file_path.suffix).lower()
    if suffix in (".jpg", ".jpeg", ".png", ".webp", ".tiff", ".tif", ".heic", ".heif"):
        try:
            from PIL import Image

            with Image.open(file_path) as img:
                exif = img.getexif()
                try:
                    exif_ifd = exif.get_ifd(0x8769)
                except Exception:
                    exif_ifd = {}
                for src, tag in ((exif_ifd, 36867), (exif_ifd, 36868), (exif, 306)):
                    raw = src.get(tag)
                    if raw:
                        return datetime.strptime(str(raw).strip(), "%Y:%m:%d %H:%M:%S").timestamp()
        except Exception:
            return None
        return None
    if suffix in (".mp4", ".mov", ".m4v", ".3gp", ".mkv", ".avi", ".webm"):
        if not shutil.which("ffprobe"):
            return None
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "quiet", "-show_entries", "format_tags=creation_time",
                 "-of", "default=nw=1:nk=1", str(file_path)],
                capture_output=True, text=True, timeout=15,
            ).stdout.strip()
            if out:
                return datetime.fromisoformat(out.replace("Z", "+00:00")).timestamp()
        except Exception:
            return None
    return None


_VIDEO_SUFFIXES = (".mp4", ".mov", ".m4v", ".3gp", ".mkv", ".avi", ".webm")


def _extract_video_duration(file_path: Path, suffix_hint: Optional[str] = None) -> Optional[float]:
    """Return a video's duration in seconds via ffprobe, or None (not a video,
    ffprobe unavailable, or the probe failed). Duration is a nice-to-have for
    the app's video-badge UI, never allowed to block or fail an upload."""
    suffix = (suffix_hint or file_path.suffix).lower()
    if suffix not in _VIDEO_SUFFIXES or not shutil.which("ffprobe"):
        return None
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(file_path)],
            capture_output=True, text=True, timeout=15,
        ).stdout.strip()
        return float(out) if out else None
    except Exception:
        return None


# Common camera/WhatsApp/screenshot filename date conventions:
# IMG_20250115_143022.jpg, IMG-20250115-WA0001.jpg, Screenshot_20250115-143022.png,
# or a bare YYYYMMDD anywhere in the name. Longer (with time) pattern first so
# it wins over the bare-date pattern when both would match.
_FILENAME_DATE_RE = re.compile(r"(\d{4})(\d{2})(\d{2})[_-](\d{2})(\d{2})(\d{2})")
_FILENAME_DATE_ONLY_RE = re.compile(r"(\d{4})(\d{2})(\d{2})")


def _parse_date_from_filename(name: str) -> Optional[float]:
    """Best-effort capture date parsed from a YYYYMMDD(-HHMMSS) pattern in the
    filename — the fallback used when a file has neither embedded EXIF/ffprobe
    metadata nor a caller-supplied original_date (common for WhatsApp-
    recompressed images, which strip EXIF)."""
    for pattern, with_time in ((_FILENAME_DATE_RE, True), (_FILENAME_DATE_ONLY_RE, False)):
        m = pattern.search(name)
        if not m:
            continue
        try:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if not (1995 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31):
                continue
            h, mi, s = (int(m.group(4)), int(m.group(5)), int(m.group(6))) if with_time else (0, 0, 0)
            return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return None


def _resolve_capture_epoch(file_path: Path, filename: str, fallback_epoch=None) -> float:
    """Resolve the best-known capture epoch, trying in order: embedded
    EXIF/ffprobe metadata, a caller-supplied fallback (e.g. the phone's real
    capture date on a sync upload), a YYYYMMDD pattern in the filename, and
    finally the current time. Always returns a usable epoch — capture-date
    bucketing and media_index recording both depend on never getting None."""
    ts = _extract_capture_epoch(file_path, suffix_hint=Path(filename).suffix)
    if ts is None and fallback_epoch and float(fallback_epoch) > 0:
        ts = float(fallback_epoch)
    if ts is None:
        ts = _parse_date_from_filename(filename)
    if ts is None:
        ts = datetime.now(timezone.utc).timestamp()
    return ts


def _apply_capture_time(file_path: Path, fallback_epoch=None) -> Optional[float]:
    """Best-effort: set the file mtime to its resolved capture time (see
    _resolve_capture_epoch) and return the epoch used. Never raises. Used
    directly by callers (e.g. the Telegram bot) that place a file themselves
    and don't need a date-bucketed destination decided in advance."""
    try:
        ts = _resolve_capture_epoch(file_path, file_path.name, fallback_epoch)
        os.utime(file_path, (ts, ts))
        return ts
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Ingestion core
# ---------------------------------------------------------------------------

class Scope(str, Enum):
    PERSONAL = "personal"
    FAMILY = "family"
    ENTERTAINMENT = "entertainment"


class IngestMode(str, Enum):
    SORTED = "sorted"    # auto-classify by extension into Photos/Videos/Documents/Others
    MIRROR = "mirror"    # write verbatim into dest.subpath, no auto-sort


@dataclass(frozen=True)
class Destination:
    scope: Scope
    owner: Optional[str] = None       # required iff scope == PERSONAL and raw_dir is None
    subpath: Optional[str] = None     # explicit subdir under the scope root; None + SORTED = auto-classify
    mode: IngestMode = IngestMode.SORTED
    raw_dir: Optional[Path] = None    # already-resolved+authorized dir (bypasses scope->dir derivation)

    def base_dir(self) -> Path:
        if self.raw_dir is not None:
            return self.raw_dir
        if self.scope == Scope.PERSONAL:
            if not self.owner:
                raise ValueError("Destination.owner is required for PERSONAL scope")
            return settings.personal_path / Path(self.owner).name
        if self.scope == Scope.FAMILY:
            return settings.family_path
        return settings.entertainment_path


@dataclass(frozen=True)
class IngestResult:
    path: Path
    sha256: str
    sorted_to: Optional[str]
    dedup_hit: bool
    bytes_written: int


class IngestStallError(Exception):
    """Raised when no data was received from the chunk source within the stall timeout."""


class IngestSizeError(Exception):
    """Raised when the incoming stream exceeds the configured max upload size."""


class IngestDiskFullError(Exception):
    """Raised when the primary drive runs out of space mid-write, or a preflight check
    finds clearly insufficient free space before starting. Found live 2026-07-16
    (full-repo audit): only the Telegram download path had a disk-space preflight --
    a full NAS drive on a normal app upload/sync previously surfaced as a raw, unhelpful
    OSError(ENOSPC) turned into a generic 500, not a clear "storage is full" message."""


async def stream_to_temp(
    chunks: AsyncIterator[bytes],
    temp_path: Path,
    *,
    max_bytes: Optional[int] = None,
    stall_timeout_s: int = 60,
) -> tuple[int, str]:
    """Stream *chunks* into *temp_path*, hashing incrementally as it writes.

    Low-level primitive shared by :func:`ingest` and any caller (e.g. the
    Telegram bot) that needs its own duplicate-handling policy instead of
    ingest()'s default dedup-skip behavior. Bounds the GAP between successive
    chunks (not total transfer time), so a slow-but-alive transfer completes
    fine while a truly stalled connection is caught within `stall_timeout_s`.

    On any error the caller is responsible for cleaning up temp_path — this
    function does not delete it, so partial state is inspectable if needed.

    Returns (bytes_written, sha256_hex).
    """
    temp_path.parent.mkdir(parents=True, exist_ok=True)
    loop = asyncio.get_running_loop()
    sha = hashlib.sha256()
    total = 0

    fd = open(temp_path, "wb", buffering=_UPLOAD_WRITE_BUF)
    try:
        iterator = chunks.__aiter__()
        while True:
            try:
                chunk = await asyncio.wait_for(iterator.__anext__(), timeout=stall_timeout_s)
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:
                raise IngestStallError(
                    f"No data received for {stall_timeout_s}s — transfer stalled"
                )
            if not chunk:
                continue
            total += len(chunk)
            if max_bytes and total > max_bytes:
                raise IngestSizeError(
                    f"Stream exceeds maximum size of {max_bytes // (1024 * 1024)} MB"
                )
            sha.update(chunk)
            try:
                await loop.run_in_executor(None, fd.write, chunk)
            except OSError as exc:
                if exc.errno == errno.ENOSPC:
                    raise IngestDiskFullError("The primary storage drive is full") from exc
                raise
        # fsync before close: this board loses power, and ext4 delayed
        # allocation can otherwise leave a "successfully uploaded" file as
        # zero bytes after a crash. Only on the success path — error paths
        # discard the temp file anyway.
        try:
            await loop.run_in_executor(None, fd.flush)
            await loop.run_in_executor(None, os.fsync, fd.fileno())
        except OSError as exc:
            if exc.errno == errno.ENOSPC:
                raise IngestDiskFullError("The primary storage drive is full") from exc
            raise
    finally:
        await loop.run_in_executor(None, fd.close)

    return total, sha.hexdigest()


async def _dedup_lookup(scope: Scope, owner: Optional[str], sha256: str) -> Optional[dict]:
    """Look up a live (non-deleted) file with this content hash in this scope —
    owner-scoped for PERSONAL, so one member's private copy never suppresses (or
    leaks the path of) another member's upload of the same bytes.

    Backed by media_index (blobs + entries) since the SQLite dedup migration.
    The old JSON ingest_hashes store — 20k-entry cap with oldest-eviction, i.e.
    silent duplicates at real family-photo scale, plus a second store that
    needed explicit invalidation on every delete path — is gone; every delete
    already flips entries.deleted, which removes the row from this lookup.

    Never raises: on index failure dedup is silently skipped (a duplicate file
    with a uniqued name is recoverable; a failed upload is not)."""
    try:
        return await media_index.find_live_by_hash(scope.value, owner, sha256)
    except Exception as exc:
        logger.warning("dedup_lookup_failed sha=%s error=%s — dedup skipped", sha256, exc)
        return None


def _infer_source(dest: Destination) -> tuple[str, Optional[str]]:
    """Best-effort (source, source_folder) fallback for callers that don't pass source/
    source_folder explicitly to ingest() — i.e. the general explicit-path upload feature
    (browse to a folder, upload here), which still legitimately writes to an arbitrary
    raw_dir. Sync no longer infers its identity from a physical "Backups/<id>" path
    (retired — sync now writes through the normal SORTED/bucket-allocated tree, merged
    with direct uploads, and passes source="sync"+source_folder explicitly instead;
    see the /upload and /upload-stream routes' `sourceFolder` query param)."""
    if dest.raw_dir is None:
        return "direct_upload", None
    return "manual_path_upload", None


async def ingest(
    chunks: AsyncIterator[bytes],
    *,
    filename: str,
    dest: Destination,
    user: Optional[dict] = None,
    size_hint: Optional[int] = None,
    original_date: Optional[float] = None,
    stall_timeout_s: int = 60,
    skip_dedup: bool = False,
    source: Optional[str] = None,
    source_folder: Optional[str] = None,
) -> IngestResult:
    """Ingest a byte stream into a NAS bucket.

    Handles: path safety, per-user authorization (if `user` is given —
    trusted internal callers like the Telegram bot may pass None to skip
    this, since they resolve ownership through their own linked-chat logic),
    filename/extension validation, stall-bounded chunked write to a
    `.uploading` temp file, incremental SHA-256, per-(scope, hash)
    deduplication, atomic rename, capture-time correction, and synchronous
    extension-based sorting.

    `source`/`source_folder` are recorded into media_index as-is when given
    (e.g. "sync"/<folder-id> from the /upload route's sourceFolder param) —
    explicit rather than inferred, since sync no longer writes to a
    physically distinct location that a Destination shape could signal.
    When omitted, falls back to `_infer_source(dest)` for the remaining
    caller that still needs it (the general explicit-path upload feature).
    """
    _require_external_storage()

    safe_name = Path(filename).name
    if not safe_name or safe_name in (".", ".."):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid filename")
    for ext in (s.lower() for s in Path(safe_name).suffixes):
        if ext in BLOCKED_EXTENSIONS:
            raise HTTPException(
                status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                f"File type '{ext}' is not allowed for security reasons.",
            )

    base_dir = dest.base_dir()
    if dest.subpath:
        target_dir = base_dir / dest.subpath
        sorted_to = None
    elif dest.mode == IngestMode.SORTED:
        folder_name = _destination_folder(Path(safe_name), base_dir=base_dir)
        target_dir = base_dir / folder_name
        sorted_to = folder_name
    else:
        target_dir = base_dir
        sorted_to = None

    resolved_dir = target_dir.resolve() if target_dir.exists() else (
        target_dir.parent.resolve() / target_dir.name
    )
    if not resolved_dir.is_relative_to(settings.nas_root.resolve()):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Path outside NAS root")
    if user is not None:
        await _authorize_path(resolved_dir, user)

    target_dir.mkdir(parents=True, exist_ok=True)
    temp_path = target_dir / (safe_name + ".uploading")
    max_bytes = settings.max_upload_bytes

    # Fast-fail preflight when the caller already knows the size (most upload paths send
    # Content-Length) -- catches the common case before a single byte is written, rather
    # than only after streaming however far the drive had room for. Not a substitute for
    # stream_to_temp's own ENOSPC handling below: free space can still be consumed by a
    # concurrent upload/sync between this check and the write, which is exactly why that
    # second guard exists too.
    if size_hint:
        try:
            free = shutil.disk_usage(str(settings.nas_root)).free
        except OSError:
            free = None
        if free is not None and size_hint > free:
            raise IngestDiskFullError("The primary storage drive is full")

    try:
        total, sha256 = await stream_to_temp(
            chunks, temp_path, max_bytes=max_bytes, stall_timeout_s=stall_timeout_s,
        )
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise

    if not skip_dedup:
        existing = await _dedup_lookup(dest.scope, dest.owner, sha256)
        if existing:
            temp_path.unlink(missing_ok=True)
            # existing["path"] is nas-root-relative (see media_index.find_live_by_hash) — every
            # other IngestResult.path in this function is an absolute
            # filesystem Path, so this must be resolved the same way or
            # callers doing path.relative_to(nas_root) (e.g. the /upload
            # route's response) crash on a dedup hit.
            existing_abs = settings.nas_root.resolve() / existing["path"].lstrip("/")
            return IngestResult(
                path=existing_abs, sha256=sha256, sorted_to=None,
                dedup_hit=True, bytes_written=total,
            )

    # Capture time is resolved from the temp file's bytes now (suffix_hint
    # carries the real extension, since temp_path is literally named
    # "<safe_name>.uploading") so a date-bucketed final directory can be
    # chosen before the rename, rather than moving the file twice.
    # Off the event loop, both of them. `_resolve_capture_epoch` opens the file with PIL to read
    # EXIF, and `_extract_video_duration` shells out to ffprobe with a 15 s timeout — run inline
    # they block the *whole* loop, so no other request can progress while one upload reads its
    # metadata. That is what made upload throughput flat from 1 to 8 concurrent uploaders:
    # measured on the dev board, 2.13 MB/s at one worker and 2.27 at eight, when the disk and
    # network were nowhere near saturated.
    loop = asyncio.get_running_loop()
    capture_epoch = await loop.run_in_executor(
        None, _resolve_capture_epoch, temp_path, safe_name, original_date
    )
    video_duration = await loop.run_in_executor(
        None, _extract_video_duration, temp_path, Path(safe_name).suffix
    )
    if sorted_to is not None:
        # SORTED mode with no explicit subpath (direct upload / Sync /
        # Telegram): nest the physical write under <category>/<YYYY>/<MM[-bN]>/
        # by capture date, not upload date, with the leaf reserved through the
        # bucket allocator (structural ≤500 files/dir). MIRROR-mode/explicit-
        # subpath writes (manual path uploads) are left exactly where the
        # caller put them — media_index, not the physical path, is what makes
        # those browsable "by folder" in the app.
        final_dir = await loop.run_in_executor(
            None, _date_bucket_dir, target_dir, capture_epoch,
        )
        final_dir.mkdir(parents=True, exist_ok=True)
    else:
        final_dir = target_dir

    # _unique_dest (rather than a raw overwrite) is a deliberate, strictly-safer
    # change from the old /upload behavior: true duplicates are already caught
    # by the hash-dedup check above, so this only affects same-name-different-
    # content collisions, where auto-suffixing beats silently destroying data.
    final_dest = _unique_dest(final_dir, safe_name)
    if not final_dest.resolve().is_relative_to(settings.nas_root.resolve()):
        temp_path.unlink(missing_ok=True)
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Path outside NAS root")

    def _rename_and_sync_dir() -> None:
        temp_path.rename(final_dest)
        # fsync the parent directory so the rename itself (the "this file
        # exists under its final name" fact) survives a power loss, not just
        # the file's bytes (already fsynced in stream_to_temp). Best-effort.
        try:
            dir_fd = os.open(final_dest.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass

    await loop.run_in_executor(None, _rename_and_sync_dir)

    try:
        os.utime(final_dest, (capture_epoch, capture_epoch))
    except OSError:
        pass
    rel_path = "/" + str(final_dest.relative_to(settings.nas_root.resolve())).replace("\\", "/")

    # Metadata index — category is either what SORTED mode already decided (sorted_to),
    # or, for MIRROR-mode writes (manual raw_dir uploads) where no physical sorting
    # happens, a metadata-only classification via the same categorizer. Never allowed
    # to fail the actual upload — see media_index.record_entry's own docstring.
    if source is None:
        source, source_folder = _infer_source(dest)
    category = sorted_to or _destination_folder(Path(safe_name), base_dir=None)
    media_type, _ = mimetypes.guess_type(safe_name)
    try:
        final_mtime: Optional[float] = final_dest.stat().st_mtime
    except OSError:
        final_mtime = None
    await media_index.record_entry(
        rel_path=rel_path,
        content_hash=sha256,
        size_bytes=total,
        scope=dest.scope.value,
        owner=dest.owner,
        category=category,
        media_type=media_type,
        source=source,
        source_folder=source_folder,
        filename=final_dest.name,
        original_name=safe_name,
        capture_date=capture_epoch,
        mtime=final_mtime,
        duration=video_duration,
    )

    return IngestResult(
        path=final_dest, sha256=sha256, sorted_to=sorted_to,
        dedup_hit=False, bytes_written=total,
    )
