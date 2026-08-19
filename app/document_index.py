"""
Document index — SQLite FTS5 full-text search for files sorted into Documents/.
Database is stored at settings.data_dir / "docs.db".

OCR strategy:
  .txt / .md / .csv / .rtf  → read file directly
  .pdf                       → pdftotext (from poppler-utils)
  .jpg / .png / .heic / ...  → tesseract OCR (eng+hin)
  anything else              → empty string

If pdftotext / tesseract is not installed, a warning is logged and the file
is indexed with an empty ocr_text — never fails permanently.
"""

import asyncio
import logging
import os
import queue
import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from fnmatch import fnmatch
from typing import Generator

from .config import settings
from . import workload
from .subprocess_runner import run_command

logger = logging.getLogger("aihomecloud.document_index")

# Keep tesseract single-threaded so N parallel OCR workers map cleanly to N cores. Multi-threaded
# tesseract × several concurrent workers oversubscribes the CPU, slowing each run enough to hit the
# OCR timeout (→ empty results). Set before any subprocess is spawned.
os.environ.setdefault("OMP_NUM_THREADS", "1")

# OCR tool availability flags — set at startup
ocr_pdftotext_available: bool = False
ocr_tesseract_available: bool = False


def check_ocr_tools() -> None:
    """Check for OCR tools at startup and set availability flags."""
    global ocr_pdftotext_available, ocr_tesseract_available
    ocr_pdftotext_available = shutil.which("pdftotext") is not None
    ocr_tesseract_available = shutil.which("tesseract") is not None
    if not ocr_pdftotext_available:
        logger.warning("pdftotext not found — PDF text extraction disabled. Install: sudo apt install poppler-utils")
    if not ocr_tesseract_available:
        logger.warning("tesseract not found — image OCR disabled. Install: sudo apt install tesseract-ocr")


_INDEXABLE_EXTENSIONS: frozenset[str] = frozenset({
    ".txt", ".md", ".csv", ".rtf", ".pdf",
    ".jpg", ".jpeg", ".png", ".heic", ".heif", ".tiff", ".tif", ".bmp",
})


# ---------------------------------------------------------------------------
# DB helpers (sync — executed via run_in_executor)
# ---------------------------------------------------------------------------

def _db_path() -> Path:
    return settings.data_dir / "docs.db"


# ---------------------------------------------------------------------------
# Connection pool — reuse up to _POOL_SIZE SQLite connections for reads/writes.
# ---------------------------------------------------------------------------

_POOL_SIZE: int = settings.document_index_pool_size
_pool: queue.Queue[sqlite3.Connection] = queue.Queue(maxsize=_POOL_SIZE)


def _new_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path()), timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # eliminates read/write contention
    conn.execute("PRAGMA synchronous=NORMAL") # safe with WAL; faster on SD/NVMe
    return conn


@contextmanager
def _get_conn() -> Generator[sqlite3.Connection, None, None]:
    """Borrow a connection from the pool; return it when done."""
    try:
        conn = _pool.get_nowait()
    except queue.Empty:
        conn = _new_conn()
    try:
        yield conn
    finally:
        try:
            _pool.put_nowait(conn)
        except queue.Full:
            conn.close()


def _close_pool() -> None:
    """Drain and close all pooled connections. Called on shutdown."""
    while not _pool.empty():
        try:
            _pool.get_nowait().close()
        except queue.Empty:
            break


def _init_db_sync() -> None:
    with _get_conn() as conn:
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS doc_index USING fts5(
                path     UNINDEXED,
                filename,
                ocr_text,
                added_by UNINDEXED,
                added_at UNINDEXED
            )
            """
        )
        conn.commit()


async def init_db() -> None:
    """Initialise the FTS5 database. Call once from main.py lifespan."""
    check_ocr_tools()
    loop = asyncio.get_running_loop()
    # Drain stale pooled connections (e.g. if data_dir changed between calls)
    await loop.run_in_executor(None, _close_pool)
    await loop.run_in_executor(None, _init_db_sync)


async def close_db() -> None:
    """Close all pooled connections. Call from main.py lifespan shutdown."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _close_pool)


# ---------------------------------------------------------------------------
# OCR helpers — async, never raise
# ---------------------------------------------------------------------------

async def _downscaled_for_ocr(file_path: Path, max_edge: int = 2200) -> Path:
    """Return a temp downscaled PNG for faster OCR (tesseract time scales with pixel count), or the
    original path if the image is already small or can't be processed. Caller deletes temp files."""
    def _make() -> Path:
        try:
            from PIL import Image, ImageOps
            with Image.open(file_path) as img:
                img = ImageOps.exif_transpose(img)
                if max(img.size) <= max_edge:
                    return file_path
                img.thumbnail((max_edge, max_edge))
                fd, tmp_name = tempfile.mkstemp(suffix=".png", prefix="ahc_ocr_")
                os.close(fd)
                tmp = Path(tmp_name)
                img.convert("RGB").save(tmp, format="PNG")
                return tmp
        except Exception:
            return file_path

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _make)


async def _extract_text(file_path: Path) -> str:
    """Return searchable text for *file_path*. Returns '' on any failure.

    Plain-text files are always read directly (no OCR required).
    PDF and image OCR is only attempted when settings.ocr_enabled is True.
    """
    if not file_path.exists():
        return ""

    ext = file_path.suffix.lower()

    # Plain-text types: read directly — always enabled, no OCR software needed
    if ext in (".txt", ".md", ".csv", ".rtf"):
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(
                None, lambda: file_path.read_text(errors="replace")[:100_000]
            )
        except Exception as exc:
            logger.warning("text_read_error file=%s error=%s", file_path.name, exc)
            return ""

    # PDF and image OCR — only when explicitly enabled (requires pdftotext/tesseract)
    if not settings.ocr_enabled:
        return ""

    # PDF: use pdftotext (poppler-utils)
    if ext == ".pdf":
        rc, stdout, stderr = await run_command(
            ["pdftotext", str(file_path), "-"], timeout=30
        )
        if rc == 0:
            return stdout[:100_000]
        if stderr == "not_found":
            logger.warning("pdftotext_missing file=%s — indexed without OCR", file_path.name)
        else:
            logger.warning(
                "pdftotext_failed file=%s rc=%d stderr=%s", file_path.name, rc, stderr[:200]
            )
        return ""

    # Images: tesseract OCR (downscale large images first — OCR time scales with pixel count)
    if ext in (".jpg", ".jpeg", ".png", ".heic", ".heif", ".tiff", ".tif", ".bmp"):
        ocr_target = await _downscaled_for_ocr(file_path)
        try:
            rc, stdout, stderr = await run_command(
                ["tesseract", str(ocr_target), "stdout", "-l", settings.ocr_languages], timeout=180
            )
        finally:
            if ocr_target != file_path:
                try:
                    ocr_target.unlink()
                except OSError:
                    pass
        if rc == 0:
            return stdout[:100_000]
        if stderr == "not_found":
            logger.warning("tesseract_missing file=%s — indexed without OCR", file_path.name)
        else:
            logger.warning(
                "tesseract_failed file=%s rc=%d stderr=%s", file_path.name, rc, stderr[:200]
            )
        return ""

    return ""


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _to_nas_path(abs_path: Path) -> str:
    """Convert an absolute filesystem path to a NAS-relative slash path (/personal/...)."""
    try:
        relative = abs_path.resolve().relative_to(settings.nas_root.resolve())
        return "/" + str(relative).replace("\\", "/")
    except ValueError:
        return str(abs_path)


# ---------------------------------------------------------------------------
# Query normalization — expand common aliases and misspellings
# ---------------------------------------------------------------------------

# Maps common user search terms to FTS5 OR queries.
# Covers Hindi/English variants and frequent misspellings.
_SEARCH_ALIASES: dict[str, str] = {
    "aadhar":    'aadhaar OR aadhar OR आधार OR uidai',
    "aadhaar":   'aadhaar OR aadhar OR आधार OR uidai',
    "aadharcard":'aadhaar OR aadhar OR आधार OR uidai',
    "pan":       'pan OR pancard OR आयकर OR "income tax"',
    "pancard":   'pan OR pancard OR आयकर OR "income tax"',
    "license":   'license OR licence OR driving OR DL',
    "licence":   'license OR licence OR driving OR DL',
    "dl":        'license OR licence OR driving OR DL',
    "passport":  'passport OR पासपोर्ट',
    "invoice":   'invoice OR bill OR receipt',
    "receipt":   'invoice OR bill OR receipt',
    "marksheet": 'marksheet OR marks OR result OR "mark sheet"',
}


def _normalize_query(raw: str) -> str:
    """Expand known aliases and add prefix matching for short single-word queries."""
    stripped = raw.strip().lower()
    if stripped in _SEARCH_ALIASES:
        return _SEARCH_ALIASES[stripped]
    # Single word under 12 chars → also try prefix match for typo tolerance
    words = stripped.split()
    if len(words) == 1 and len(stripped) <= 12 and stripped.isalpha():
        return f'{stripped} OR {stripped}*'
    return raw.strip()


# ---------------------------------------------------------------------------
# Sync DB operations — called via run_in_executor
# ---------------------------------------------------------------------------

def _upsert_sync(nas_path: str, filename: str, ocr_text: str, added_by: str) -> None:
    added_at = datetime.now(timezone.utc).isoformat()
    with _get_conn() as conn:
        # Replace any existing entry (path is the unique key)
        conn.execute("DELETE FROM doc_index WHERE path = ?", (nas_path,))
        conn.execute(
            "INSERT INTO doc_index (path, filename, ocr_text, added_by, added_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (nas_path, filename, ocr_text, added_by, added_at),
        )
        conn.commit()


def _escape_like(value: str) -> str:
    """Escape SQL LIKE wildcards in user-controlled text used to build a pattern — mirrors
    media_index._like_prefix's escaping. Must be paired with `ESCAPE '\\'` on the LIKE clause."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _search_sync(query: str, limit: int, user_role: str, username: str) -> list[dict]:
    fts_query = _normalize_query(query)
    word = query.strip()
    with _get_conn() as conn:
        if user_role == "admin":
            fts_rows = conn.execute(
                """
                SELECT path, filename, added_by, added_at,
                       snippet(doc_index, 2, '', '', '…', 10) AS snippet
                FROM doc_index
                WHERE doc_index MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (fts_query, limit),
            ).fetchall()

            # Supplemental: single-word query → also match all docs in that user's
            # personal folder (e.g. "paras" → /personal/Paras/...).
            # path is UNINDEXED so FTS can't find it; a LIKE query fills the gap.
            path_rows: list = []
            if len(word.split()) == 1 and word.isalpha():
                path_rows = conn.execute(
                    """
                    SELECT path, filename, added_by, added_at, '' AS snippet
                    FROM doc_index
                    WHERE LOWER(path) LIKE ?
                    LIMIT 50
                    """,
                    (f"/personal/{word.lower()}/%",),
                ).fetchall()

            # Merge FTS results first, then path results; deduplicate by path.
            seen: set[str] = set()
            merged: list[dict] = []
            for row in list(fts_rows) + list(path_rows):
                p = row["path"]
                if p not in seen:
                    seen.add(p)
                    merged.append(dict(row))
            return merged[:limit]
        else:
            # Member scope: own personal Documents + shared Documents. username is a
            # user-chosen display name, not guaranteed alnum (unlike the admin branch's
            # word.isalpha()-guarded query above) — a name containing LIKE wildcards would
            # otherwise broaden ("_" matches any single char, e.g. "a_b" matching "axb") or
            # narrow the match, potentially surfacing another member's document hits.
            own_prefix = f"/personal/{_escape_like(username)}/%"
            shared_prefix = "/family/%"
            rows = conn.execute(
                """
                SELECT path, filename, added_by, added_at,
                       snippet(doc_index, 2, '', '', '…', 10) AS snippet
                FROM doc_index
                WHERE doc_index MATCH ?
                  AND (path LIKE ? ESCAPE '\\' OR path LIKE ?)
                ORDER BY rank
                LIMIT ?
                """,
                (fts_query, own_prefix, shared_prefix, limit),
            ).fetchall()
            return [dict(row) for row in rows]


def _remove_sync(nas_path: str) -> None:
    with _get_conn() as conn:
        try:
            conn.execute("DELETE FROM doc_index WHERE path = ?", (nas_path,))
            conn.commit()
        except sqlite3.OperationalError:
            pass  # table not yet created — nothing to remove


def _remove_prefix_sync(nas_prefix: str) -> int:
    """Delete all indexed docs whose path starts with *nas_prefix*."""
    with _get_conn() as conn:
        try:
            cur = conn.execute("DELETE FROM doc_index WHERE path LIKE ?", (f"{nas_prefix}%",))
            conn.commit()
            return cur.rowcount or 0
        except sqlite3.OperationalError:
            return 0  # table not yet created — nothing to remove


def _remove_by_filename_patterns_sync(patterns: list[str]) -> int:
    """Delete indexed docs matching any shell-style filename pattern."""
    if not patterns:
        return 0
    with _get_conn() as conn:
        rows = conn.execute("SELECT path, filename FROM doc_index").fetchall()
        to_remove = [r["path"] for r in rows if any(fnmatch(r["filename"], p) for p in patterns)]
        if not to_remove:
            return 0
        conn.executemany("DELETE FROM doc_index WHERE path = ?", [(p,) for p in to_remove])
        conn.commit()
        return len(to_remove)


def _remove_missing_sync() -> int:
    """Delete indexed entries whose filesystem paths no longer exist."""
    with _get_conn() as conn:
        removed = 0
        rows = conn.execute("SELECT path FROM doc_index").fetchall()
        for row in rows:
            nas_path = row["path"]
            abs_path = settings.nas_root / nas_path.lstrip("/")
            if not abs_path.exists():
                conn.execute("DELETE FROM doc_index WHERE path = ?", (nas_path,))
                removed += 1
        if removed:
            conn.commit()
        return removed


# ---------------------------------------------------------------------------
# Public async API
# ---------------------------------------------------------------------------

def _nas_paths_with_ocr_sync() -> set:
    with _get_conn() as conn:
        return {row[0] for row in conn.execute("SELECT path FROM doc_index WHERE length(ocr_text) > 0")}


async def nas_paths_with_ocr() -> set:
    """NAS paths already indexed with NON-EMPTY OCR text — used to skip well-indexed files (and thus
    re-OCR only new files + prior failures/empties) on a manual re-scan."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _nas_paths_with_ocr_sync)


#: Never starve indexing outright — after this long a document is indexed even if the household
#: is still busy. Paras's constraint: a delay of up to 24 hours is fine, and the house is reliably
#: idle for 4-6 hours overnight, so there is always a window to drain into. Waiting a long time is
#: therefore free; interrupting someone is not.
_OCR_MAX_DEFER_SECONDS = 24 * 60 * 60.0


async def index_document(path: str, filename: str, added_by: str) -> None:
    """
    Async: OCR and index a document at *path*.
    *path* may be an absolute filesystem path or a NAS-relative path (/personal/…).
    Never raises — errors are logged as warnings.
    """
    try:
        abs_path = (
            Path(path)
            if Path(path).is_absolute()
            else settings.nas_root / path.lstrip("/")
        )
        nas_path = _to_nas_path(abs_path)
        # Wait for the family to stop before spending a tesseract subprocess on this file.
        # Uploads fire this per file via create_task, so during a bulk sync the board would
        # otherwise OCR the very files it is still receiving — measured on the Cubie (937 MB,
        # already in swap at idle) that is the difference between 10 MB/s and 4.5 MB/s.
        # Bounded so a household that is never idle still gets its documents indexed eventually.
        await workload.gate(f"ocr:{nas_path}", max_wait=_OCR_MAX_DEFER_SECONDS)
        ocr_text = await _extract_text(abs_path)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _upsert_sync, nas_path, filename, ocr_text, added_by)
        logger.info(
            "indexed_document path=%s added_by=%s ocr_chars=%d",
            nas_path, added_by, len(ocr_text),
        )
    except Exception as exc:
        logger.warning("index_document_failed path=%s error=%s", path, exc)


async def search_documents(
    query: str,
    limit: int = 5,
    user_role: str = "member",
    username: str = "",
) -> list[dict]:
    """
    Async FTS5 full-text search.

    Scope:
      admin  → all indexed documents
      member → own (/personal/{username}/…) + shared (/shared/…) only
    """
    if not query.strip():
        return []
    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, _search_sync, query, limit, user_role, username
        )
    except sqlite3.OperationalError as exc:
        # Malformed FTS query (bare AND/OR, unclosed quotes, etc.)
        logger.warning("search_documents_bad_query query=%r error=%s", query, exc)
        return []


async def remove_document(path: str) -> None:
    """Async: remove a document entry from the index."""
    if Path(path).is_absolute():
        path = _to_nas_path(Path(path))
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _remove_sync, path)


async def remove_documents_by_prefix(path_prefix: str) -> int:
    """Async: remove all indexed docs under a NAS path prefix."""
    if Path(path_prefix).is_absolute():
        path_prefix = _to_nas_path(Path(path_prefix))
    nas_prefix = path_prefix.rstrip("/") + "/"
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _remove_prefix_sync, nas_prefix)


async def remove_documents_by_filename_patterns(patterns: list[str]) -> int:
    """Async: remove index rows matching shell-style filename patterns."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _remove_by_filename_patterns_sync, patterns)


async def remove_missing_documents() -> int:
    """Async: prune stale index entries whose files are gone."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _remove_missing_sync)


def is_indexable_document_path(path: Path) -> bool:
    """Return True when *path* should be indexed by document_index."""
    return path.is_file() and path.suffix.lower() in _INDEXABLE_EXTENSIONS


async def index_documents_under_path(root_path: str, added_by: str) -> int:
    """Recursively index all supported document files under *root_path*."""
    root = Path(root_path) if Path(root_path).is_absolute() else settings.nas_root / root_path.lstrip("/")
    if not root.exists():
        return 0

    indexed = 0
    if root.is_file():
        if is_indexable_document_path(root):
            await index_document(str(root), root.name, added_by)
            indexed += 1
        return indexed

    for p in root.rglob("*"):
        if is_indexable_document_path(p):
            try:
                await asyncio.wait_for(
                    index_document(str(p), p.name, added_by),
                    timeout=120,
                )
            except asyncio.TimeoutError:
                logger.warning("index_timeout path=%s — skipping", p)
            indexed += 1
    return indexed


def _list_recent_sync(limit: int, user_role: str, username: str) -> list[dict]:
    with _get_conn() as conn:
        if user_role == "admin":
            rows = conn.execute(
                """
                SELECT path, filename, added_by, added_at
                FROM doc_index
                ORDER BY added_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        else:
            # Same scope rule search_documents already applied: own personal folder plus shared.
            # This listing had NO filter at all, so it returned the most recent documents belonging
            # to every member — and the Telegram bot then offers "reply with a number" to download
            # them. Anyone linked to the bot could read another member's documents.
            rows = conn.execute(
                """
                SELECT path, filename, added_by, added_at
                FROM doc_index
                WHERE LOWER(path) LIKE ? OR LOWER(path) LIKE '/shared/%'
                ORDER BY added_at DESC
                LIMIT ?
                """,
                (f"/personal/{(username or '').lower()}/%", limit),
            ).fetchall()
        return [dict(row) for row in rows]


async def list_recent_documents(
    limit: int = 10, user_role: str = "member", username: str = ""
) -> list[dict]:
    """
    The *limit* most recently indexed documents, newest first, scoped to the caller.

    Defaults to `member` with an empty username — i.e. shared documents only. A caller that forgets
    to identify itself gets less, never more; the previous signature took only `limit` and returned
    everything, which is how the Telegram bot ended up exposing every member's private documents.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _list_recent_sync, limit, user_role, username)
