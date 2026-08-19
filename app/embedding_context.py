"""
Extra text worth embedding, gathered from the indexes we already maintain.

`embedding.humanise_path()` can only work with what is in the path, and measured against the real
library that is often nothing: a third of files are camera exports like `IMG_20260428_083411.jpg`,
and `original_name` turns out to be **identical to the filename** for all 445 media rows on the
production board — so it adds no signal at all despite being fully populated.

What genuinely helps, measured on the same library:

* **`ocr_text` from `docs.db`** — 63 of 97 rows have real text, and most of them are *screenshots
  sitting in the photo library* rather than documents. This is the difference between "screenshots"
  returning three arbitrary photos and returning the actual screenshots.
* **`capture_date` from `media.db`** — populated for all 445 rows. Rendered as "April 2026" it makes
  date queries work.
* **`category`** — Photos / Videos / Movies / Series. Coarse, but it separates a home video from a
  downloaded film, which the path often does not.

Deliberately **not** used: `source` (`direct_upload` / `sync` — an implementation detail no one would
search for) and `original_name` (a duplicate of the filename, as above). Adding either would dilute
a short embedding string with tokens that carry no information.

**This cannot fix content queries on photos.** "beach holiday" or "kids playing" need something that
has looked at the pixels; no amount of metadata will do it. That is a separate question with a
separate cost, and this module makes no attempt at it.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Iterable, Sequence

logger = logging.getLogger("aihomecloud.embedding_context")

# bge-small truncates at 512 tokens. Path text is ~8 words, so there is plenty of room, but OCR of
# a dense screenshot can run to thousands of characters — most of it boilerplate from the tail of
# the image. Keeping the head bounded means one verbose screenshot cannot crowd out the path text
# it is being appended to.
MAX_OCR_CHARS = 600

# SQLite's default limit is 999 bound variables per statement.
_SQL_CHUNK = 400

_CATEGORY_WORDS = {
    "Photos": "photo",
    "Videos": "video",
    "Movies": "movie",
    "Series": "series",
    "Others": "",
}


def _chunks(items: Sequence[str], size: int = _SQL_CHUNK) -> Iterable[Sequence[str]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _open_ro(db: Path) -> sqlite3.Connection | None:
    """Read-only, and never fatal — enrichment is an improvement, not a dependency."""
    try:
        if not Path(db).exists():
            return None
        return sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        logger.warning("embedding_context_open_failed db=%s error=%s", db, exc)
        return None


def _describe_capture(epoch: int | None) -> str:
    """`1777365251` → `April 2026`. A timestamp is unsearchable; a month name is not."""
    if not epoch:
        return ""
    try:
        return time.strftime("%B %Y", time.localtime(int(epoch)))
    except (ValueError, OSError, OverflowError):
        return ""


def _media_extras(paths: Sequence[str], media_db: Path) -> dict[str, str]:
    conn = _open_ro(media_db)
    if conn is None:
        return {}
    out: dict[str, str] = {}
    try:
        for chunk in _chunks(paths):
            marks = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"""
                SELECT b.rel_path, e.category, e.capture_date
                FROM blobs b JOIN entries e ON e.blob_id = b.id
                WHERE e.deleted = 0 AND b.rel_path IN ({marks})
                """,
                tuple(chunk),
            ).fetchall()
            for rel_path, category, capture in rows:
                bits = [
                    _CATEGORY_WORDS.get(category or "", ""),
                    _describe_capture(capture),
                ]
                text = " ".join(b for b in bits if b)
                if text:
                    out[rel_path] = text
    except sqlite3.Error as exc:
        logger.warning("embedding_context_media_failed error=%s", exc)
    finally:
        conn.close()
    return out


def _ocr_extras(paths: Sequence[str], docs_db: Path) -> dict[str, str]:
    conn = _open_ro(docs_db)
    if conn is None:
        return {}
    out: dict[str, str] = {}
    try:
        for chunk in _chunks(paths):
            marks = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"SELECT path, ocr_text FROM doc_index WHERE path IN ({marks})",
                tuple(chunk),
            ).fetchall()
            for rel_path, ocr in rows:
                if not ocr:
                    continue
                # OCR arrives with hard newlines mid-sentence; collapsing whitespace is what turns
                # it back into something a sentence-embedding model can read.
                text = " ".join(str(ocr).split())[:MAX_OCR_CHARS]
                if text:
                    out[rel_path] = text
    except sqlite3.Error as exc:
        logger.warning("embedding_context_ocr_failed error=%s", exc)
    finally:
        conn.close()
    return out


def extras_for(paths: Sequence[str], media_db: Path, docs_db: Path) -> dict[str, str]:
    """
    Extra text per path, batched — two queries per chunk rather than two per file.

    A path missing from both indexes simply gets no entry, and the caller falls back to the path
    text alone. Enrichment never removes anything.
    """
    if not paths:
        return {}
    unique = list(dict.fromkeys(paths))
    media = _media_extras(unique, media_db)
    ocr = _ocr_extras(unique, docs_db)

    merged: dict[str, str] = {}
    for rel_path in unique:
        # OCR last: it is the most specific signal, and on a truncating model the tokens nearest
        # the front survive. Category and date are short, so both still fit.
        bits = [media.get(rel_path, ""), ocr.get(rel_path, "")]
        text = " ".join(b for b in bits if b)
        if text:
            merged[rel_path] = text
    return merged
