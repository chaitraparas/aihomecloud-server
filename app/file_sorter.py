"""
File sorting — InboxWatcher polls .inbox/ directories every 30 seconds and
auto-sorts files into Photos / Videos / Documents / Others based on extension.
"""

import asyncio
import logging
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import settings
from . import workload

logger = logging.getLogger("aihomecloud.file_sorter")

# Extension → destination folder name (lowercase extensions only)
SORT_RULES: dict[str, str] = {
    # Photos
    ".jpg": "Photos", ".jpeg": "Photos", ".png": "Photos", ".gif": "Photos",
    ".heic": "Photos", ".heif": "Photos", ".webp": "Photos", ".tiff": "Photos",
    ".tif": "Photos", ".bmp": "Photos", ".raw": "Photos", ".cr2": "Photos",
    ".nef": "Photos", ".arw": "Photos", ".dng": "Photos",
    # Videos
    ".mp4": "Videos", ".mkv": "Videos", ".mov": "Videos", ".avi": "Videos",
    ".wmv": "Videos", ".flv": "Videos", ".ts": "Videos", ".m2ts": "Videos",
    ".mts": "Videos", ".m4v": "Videos", ".3gp": "Videos", ".webm": "Videos",
    # Documents
    ".pdf": "Documents", ".doc": "Documents", ".docx": "Documents",
    ".xls": "Documents", ".xlsx": "Documents", ".ppt": "Documents",
    ".pptx": "Documents", ".txt": "Documents", ".md": "Documents",
    ".csv": "Documents", ".odt": "Documents", ".ods": "Documents",
    ".odp": "Documents", ".rtf": "Documents",
}

# Keywords in filename stem (lowercase) that identify a document-style photo.
# Matched as substrings — keywords are chosen to avoid common false positives
# (e.g. "pan" is excluded because it matches "panorama", "japan", etc.).
DOC_KEYWORDS: frozenset[str] = frozenset({
    # Indian identity documents
    "aadhaar", "aadhar", "adhaar", "adhar",
    "pancard", "pan_card", "pan-card",
    "passport",
    "driving", "drivinglicence", "drivinglicense",
    "voter", "voterid", "voter_id",
    # Financial documents
    "invoice", "receipt", "bill",
    "statement", "salary", "payslip",
    "gst", "challan",
    "insurance", "policy",
    # Legal / education
    "certificate", "marksheet", "degree", "transcript",
    "property", "agreement", "contract",
    "affidavit",
    # Medical
    "prescription",
    # Generic document indicators
    "scanned", "document",
    # Pre-existing terms kept for compatibility
    "license", "licence",
})

# WhatsApp photo filenames: IMG-YYYYMMDD-WAXXXX — always a camera photo, never a doc.
_WA_PHOTO_RE = re.compile(r"^img-\d{8}-wa\d{4}")

# WhatsApp forwarded document filenames: DOC-YYYYMMDD-WAXXXX — treat as document.
_WA_DOC_RE = re.compile(r"^doc-\d{8}-wa\d{4}")

# Android screenshot naming: Screenshot_YYYYMMDD-HHMMSS or Screenshot_YYYY…
_SCREENSHOT_RE = re.compile(r"^screenshot[_\-\s]")

# Pre-compiled word-boundary regex for DOC_KEYWORDS to avoid false positives.
# Uses \b which treats underscore as a word character, so "bill_murray" won't
# match "bill". Compile once at module load for performance.
_DOC_KW_RE = re.compile(
    r"(?<![a-z0-9])(" + "|".join(re.escape(kw) for kw in sorted(DOC_KEYWORDS, key=len, reverse=True)) + r")(?![a-z0-9])"
)

# Entertainment sub-folder routing (applied when base_dir is entertainment_path)
ENTERTAINMENT_SORT_RULES: dict[str, str] = {
    ".mp4": "Movies", ".mkv": "Movies", ".avi": "Movies", ".mov": "Movies",
    ".m4v": "Movies", ".wmv": "Movies",
    ".ts": "Series", ".m2ts": "Series", ".mts": "Series",
    ".mp3": "Music", ".flac": "Music", ".aac": "Music", ".wav": "Music",
    ".ogg": "Music", ".m4a": "Music",
}

# Every name Sort ever creates as a destination folder. sort_folder_now() must never be
# invoked with a folder whose own name is one of these -- _sort_file() computes
# category_dir = base_dir / folder_name, so running it directly inside e.g. a "Photos"
# folder creates a nested "Photos/Photos/<year>/<month>/" and moves every file one level
# deeper, making the folder look empty at the level the user is actually viewing (found
# live 2026-07-26: real user photos were never deleted, just nested one level down).
RESERVED_CATEGORY_NAMES: frozenset[str] = frozenset(
    set(SORT_RULES.values()) | set(ENTERTAINMENT_SORT_RULES.values()) | {"Others"}
)

_MIN_AGE_SECONDS = 30          # file must be at least this old (not still uploading)
_SKIP_SUFFIXES = frozenset({".uploading", ".part", ".tmp"})


def _destination_folder(file_path: Path, base_dir: Path | None = None) -> str:
    """Return the target sub-folder name for *file_path*.

    Classification strategy (keyword-only, no size threshold):

    1. Entertainment base_dir → ENTERTAINMENT_SORT_RULES (Movies/Series/Music/Others).
    2. Extension not in SORT_RULES → Others.
    3. Non-photo extensions (video, document, etc.) → as per SORT_RULES.
    4. Photo extensions → apply document-photo detection:
       a. WhatsApp camera photos (IMG-YYYYMMDD-WAXXXX) → Photos  [explicit early return]
       b. WhatsApp forwarded docs (DOC-YYYYMMDD-WAXXXX) → Documents
       c. Android screenshots (Screenshot_…) → Documents
       d. DOC_KEYWORDS substring match in filename stem → Documents
       e. Everything else → Photos

    File size is intentionally NOT used — WhatsApp-compressed photos (~200–500 KB)
    are not documents, and phone photos of ID cards (3–8 MB) are.
    """
    # Entertainment-specific routing
    if base_dir is not None:
        try:
            from .config import settings as _s
            if base_dir == _s.entertainment_path:
                ext = file_path.suffix.lower()
                return ENTERTAINMENT_SORT_RULES.get(ext, "Others")
        except Exception:
            pass

    ext = file_path.suffix.lower()
    folder = SORT_RULES.get(ext, "Others")

    # Document-photo override for image extensions
    if folder == "Photos":
        stem_lower = file_path.stem.lower()

        # WhatsApp camera photos are never documents — short-circuit immediately
        if _WA_PHOTO_RE.match(stem_lower):
            return "Photos"

        # WhatsApp forwarded documents, screenshots, and keyword-named files → Documents
        if (
            _WA_DOC_RE.match(stem_lower)
            or _SCREENSHOT_RE.match(stem_lower)
            or bool(_DOC_KW_RE.search(stem_lower))
        ):
            folder = "Documents"

    return folder


def _date_bucket_dir(category_dir: Path, capture_epoch: float) -> Path:
    """Return the YYYY/<MM[-bN]> directory (nested, not flat MM-YYYY) under
    *category_dir* for a file captured at *capture_epoch*. The month leaf is
    reserved through media_index.allocate_bucket_sync — a transactional
    counter capped at media_index.BUCKET_LIMIT files per directory — which
    replaces the old scandir-count-then-write threshold check (racy under
    concurrent uploads, and 1800 was hygiene-only, not a guarantee). Nesting
    keeps each directory's own child count small (a decade of use is ~10
    year-dirs, each holding <=12 month-dirs).

    If the allocator itself fails (media.db unavailable), falls back to the
    plain part-1 month directory with a warning — same doctrine as
    media_index.record_entry: index trouble must never fail the file write
    it's placing; the nightly reconciler repairs the counters."""
    from . import media_index  # deferred: keep this module import-light for tests

    dt = datetime.fromtimestamp(capture_epoch, tz=timezone.utc)
    year = f"{dt.year:04d}"
    month = f"{dt.month:02d}"
    try:
        rel = category_dir.resolve().relative_to(settings.nas_root.resolve())
        key_base = str(rel).replace("\\", "/")
    except (OSError, ValueError):
        key_base = str(category_dir)
    try:
        leaf = media_index.allocate_bucket_sync(f"{key_base}/{year}/{month}")
    except Exception as exc:
        logger.warning(
            "bucket_allocator_failed key=%s/%s/%s error=%s — falling back to part-1 dir",
            key_base, year, month, exc,
        )
        leaf = month
    return category_dir / year / leaf


def _unique_dest(dest_dir: Path, name: str) -> Path:
    """Return a destination path inside *dest_dir* that does not already exist."""
    dest = dest_dir / name
    if not dest.exists():
        return dest
    stem = Path(name).stem
    suffix = Path(name).suffix
    counter = 2
    while True:
        candidate = dest_dir / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def _sort_file(file_path: Path, base_dir: Path, *, check_age: bool = True) -> Optional[Path]:
    """
    Move *file_path* from .inbox/ to the appropriate sub-folder under *base_dir*.
    Returns the destination Path on success; None if skipped or an error occurred.
    Sort failures are logged as warnings — the file stays in .inbox/ so it can be
    retried on the next pass.
    """
    try:
        if check_age:
            # Skip files with upload-in-progress suffixes
            if any(file_path.name.endswith(s) for s in _SKIP_SUFFIXES):
                return None
            age = time.time() - file_path.stat().st_mtime
            if age < _MIN_AGE_SECONDS:
                return None  # file still being written; skip this pass

        folder_name = _destination_folder(file_path, base_dir)
        category_dir = base_dir / folder_name

        # Date-bucket through the same allocator every other write path uses
        # (structural ≤500/dir guarantee) — keyed on the file's real capture
        # time (EXIF/ffprobe/filename), falling back to its mtime. Deferred
        # import: ingest imports this module at load time.
        from .ingest import _resolve_capture_epoch

        capture_epoch = _resolve_capture_epoch(
            file_path, file_path.name, fallback_epoch=file_path.stat().st_mtime,
        )
        dest_dir = _date_bucket_dir(category_dir, capture_epoch)
        dest_dir.mkdir(parents=True, exist_ok=True)

        dest = _unique_dest(dest_dir, file_path.name)
        shutil.move(str(file_path), str(dest))
        logger.info(
            "sorted file=%s folder=%s dest=%s",
            file_path.name, folder_name, dest.name,
        )
        return dest

    except Exception as exc:
        logger.warning(
            "sort_failed file=%s error=%s — file stays in inbox",
            file_path.name, exc,
        )
        return None


async def _try_index_document(dest: Path, added_by: str) -> None:
    """Trigger document indexing after sorting to Documents/. Stub until TASK-P2-02."""
    try:
        from .document_index import index_document  # available after TASK-P2-02
        await index_document(str(dest), dest.name, added_by)
    except ImportError:
        pass  # document_index module not yet available
    except Exception as exc:
        logger.warning("index_document failed path=%s error=%s", dest, exc)


def _collect_inboxes() -> list[tuple[Path, Path]]:
    """
    Return list of (inbox_dir, base_dir) pairs to watch.
    base_dir is the parent of .inbox/ — sorted outputs go directly under it.
    """
    inboxes: list[tuple[Path, Path]] = []

    # family/.inbox/
    family_inbox = settings.family_path / ".inbox"
    if family_inbox.is_dir():
        inboxes.append((family_inbox, settings.family_path))

    # entertainment/.inbox/
    entertainment_inbox = settings.entertainment_path / ".inbox"
    if entertainment_inbox.is_dir():
        inboxes.append((entertainment_inbox, settings.entertainment_path))

    # personal/{username}/.inbox/
    if settings.personal_path.is_dir():
        for user_dir in settings.personal_path.iterdir():
            if user_dir.is_dir():
                inbox = user_dir / ".inbox"
                if inbox.is_dir():
                    inboxes.append((inbox, user_dir))

    return inboxes


async def _run_sort_pass() -> None:
    """Single pass: sort all ready files across all known .inbox/ directories."""
    loop = asyncio.get_running_loop()
    for inbox, base_dir in _collect_inboxes():
        try:
            entries = list(inbox.iterdir())
        except OSError:
            continue

        for file_path in entries:
            if not file_path.is_file():
                continue
            dest = await loop.run_in_executor(
                None,
                lambda p=file_path, b=base_dir: _sort_file(p, b, check_age=True),
            )
            # Date-bucketing means the file's direct parent is a month dir,
            # not the category dir — check the whole path for Documents/.
            if dest is not None and "Documents" in dest.parts:
                # Best-effort indexing — never blocks the watcher
                asyncio.create_task(
                    _try_index_document(dest, base_dir.name),
                    name=f"index_doc_{dest.name}",
                )


def _sort_folder_sync(folder: Path) -> tuple[int, int, int, list[Path]]:
    """Recursively sort files in *folder* without age checks."""
    moved = 0
    skipped = 0
    failed = 0
    docs: list[Path] = []
    category_dirs = {"Photos", "Videos", "Documents", "Others", ".inbox"}

    try:
        entries = list(folder.rglob("*"))
    except OSError:
        return moved, skipped, failed, docs

    for entry in entries:
        if not entry.is_file():
            skipped += 1
            continue

        # Skip files already inside known category folders to avoid churn.
        try:
            rel_parent_parts = entry.relative_to(folder).parent.parts
        except ValueError:
            rel_parent_parts = ()
        if any(part in category_dirs for part in rel_parent_parts):
            skipped += 1
            continue

        dest = _sort_file(entry, folder, check_age=False)
        if dest is None:
            failed += 1
            continue

        moved += 1
        if "Documents" in dest.parts:
            docs.append(dest)

    return moved, skipped, failed, docs


async def sort_folder_now(folder: Path, added_by: str) -> dict[str, int]:
    """
    Manually sort all top-level files inside *folder* into category subfolders.
    Intended for one-shot ingestion of existing dumps (e.g. RawData).
    """
    loop = asyncio.get_running_loop()
    moved, skipped, failed, docs = await loop.run_in_executor(
        None, lambda: _sort_folder_sync(folder)
    )

    indexed = 0
    for doc in docs:
        await _try_index_document(doc, added_by)
        indexed += 1

    return {
        "moved": moved,
        "skipped": skipped,
        "failed": failed,
        "indexed": indexed,
    }


class InboxWatcher:
    """Background async task that polls .inbox/ directories every *interval* seconds."""

    def __init__(self, interval: int = 30) -> None:
        self._interval = interval
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="inbox_watcher")
        logger.info("InboxWatcher started interval=%ds", self._interval)

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        logger.info("InboxWatcher stopped")

    async def _loop(self) -> None:
        while True:
            # Sorting moves files around the same disk the user is reading from.
            await workload.gate("inbox_sort", max_wait=24 * 60 * 60)
            try:
                await _run_sort_pass()
            except Exception as exc:
                logger.error("InboxWatcher pass error: %s", exc)
            await asyncio.sleep(self._interval)


# Module-level singleton — started/stopped from main.py lifespan
_watcher = InboxWatcher()


def get_watcher() -> InboxWatcher:
    return _watcher
