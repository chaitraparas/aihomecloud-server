"""
Media index — SQLite metadata layer for every file that lands on the NAS via
ingest(), regardless of source (direct app upload, Sync, Telegram).

Why this exists: today a file's category (Personal/Family/Entertainment),
media type, and which source/sync-folder it came from are recorded NOWHERE —
the only place any of that lives is the file's physical folder path. That
means: no way to browse a unified timeline across scopes, no way to
re-categorize a file after the fact, and no way to tell a synced file apart
from a directly-uploaded one once both have landed in the same Photos/
folder. This module is a queryable index alongside the filesystem, not a
replacement for it — filesystem-as-truth still holds; this DB is rebuildable
by walking the tree (see the Phase 0 migration script) and is safe to delete
and regenerate if it's ever lost or corrupted.

Database is stored at settings.data_dir / "media.db". Connection pool +
pragma choices mirror document_index.py (docs.db) for consistency and
because both were sized the same way: this backend's eventual deployment
target is a 1GB-RAM SBC, so pooled connections stay small (SQLite's default
cache_size of ~2MB/connection is left as-is, not increased) and every
DB write is wrapped so it can never fail the actual file write it's
describing — this index being briefly stale or missing an entry is fully
recoverable; losing an upload because of a DB hiccup is not.
"""

import asyncio
import logging
import os
import queue
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator, Optional, Sequence

from .config import settings

logger = logging.getLogger("aihomecloud.media_index")


def _db_path() -> Path:
    return settings.data_dir / "media.db"


# ---------------------------------------------------------------------------
# Connection pool (mirrors document_index.py's pattern)
# ---------------------------------------------------------------------------

_POOL_SIZE: int = settings.media_index_pool_size
_pool: "queue.Queue[sqlite3.Connection]" = queue.Queue(maxsize=_POOL_SIZE)


def _new_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path()), timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")    # eliminates read/write contention
    conn.execute("PRAGMA synchronous=NORMAL")  # safe with WAL; faster on SD/NVMe
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
    while not _pool.empty():
        try:
            _pool.get_nowait().close()
        except queue.Empty:
            break


def _init_db_sync() -> None:
    with _get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS blobs (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                content_hash TEXT NOT NULL,
                rel_path     TEXT UNIQUE NOT NULL,
                size_bytes   INTEGER NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_blobs_hash ON blobs(content_hash)")
        # Where each person got to in each film. Keyed by (username, entry) so resume follows the
        # person across devices — the whole point of storing it on the board rather than in a phone.
        # Position is seconds, not a percentage: a re-encoded file changes length, and a percentage
        # would silently resume somewhere else.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS playback_positions (
                username   TEXT NOT NULL,
                entry_id   INTEGER NOT NULL,
                position   REAL NOT NULL,
                duration   REAL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (username, entry_id)
            )
            """
        )
        # mtime lets the nightly reconciler skip unchanged files (same rel_path,
        # size_bytes, mtime) instead of re-hashing the whole library every night —
        # a full SHA-256 pass over everything is not viable on the 1GB-RAM target.
        blob_cols = {row[1] for row in conn.execute("PRAGMA table_info(blobs)")}
        if "mtime" not in blob_cols:
            conn.execute("ALTER TABLE blobs ADD COLUMN mtime INTEGER")
        # Playback-position sync columns (additive, 2026-08-11).
        #
        # `version` is a synchronisation counter this board assigns, incremented on every applied
        # write. It says "the stored state changed"; it is NOT a wall-clock claim and must never be
        # used to decide which device's state is newer.
        #
        # `client_updated_at` is epoch **milliseconds supplied by the reporting client**, and it is
        # the ONLY value conflict resolution compares. That is deliberate: these boards are SBCs
        # with no RTC — their clock comes from NTP and can be wrong or jump backwards — so
        # comparing a phone's clock against this board's clock compares unrelated timebases.
        # Comparing one client's timestamp against another client's is at least like-for-like.
        pos_cols = {row[1] for row in conn.execute("PRAGMA table_info(playback_positions)")}
        if "version" not in pos_cols:
            conn.execute("ALTER TABLE playback_positions ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
        if "client_updated_at" not in pos_cols:
            conn.execute("ALTER TABLE playback_positions ADD COLUMN client_updated_at INTEGER NOT NULL DEFAULT 0")
        # Bucket allocator state — the structural "≤ N files per physical
        # directory" guarantee. Every date-bucketed write path asks
        # allocate_bucket_sync() for its directory; the count here is the
        # allocation counter (repaired against reality by the reconciler),
        # replacing the old racy scandir-threshold check in file_sorter.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS buckets (
                bucket_key TEXT NOT NULL,
                part       INTEGER NOT NULL DEFAULT 1,
                count      INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (bucket_key, part)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS entries (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                blob_id       INTEGER NOT NULL REFERENCES blobs(id),
                scope         TEXT NOT NULL,
                owner         TEXT,
                category      TEXT NOT NULL,
                media_type    TEXT,
                source        TEXT NOT NULL,
                source_folder TEXT,
                filename      TEXT NOT NULL,
                original_name TEXT,
                capture_date  INTEGER,
                ingest_date   INTEGER NOT NULL,
                deleted       INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        # Video duration in seconds (NULL for photos, or videos ingested before this
        # column existed — backfilled separately, see scripts/backfill_media_index.py).
        entry_cols = {row[1] for row in conn.execute("PRAGMA table_info(entries)")}
        if "duration" not in entry_cols:
            conn.execute("ALTER TABLE entries ADD COLUMN duration REAL")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_entries_timeline "
            "ON entries(scope, owner, capture_date DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_entries_folder "
            "ON entries(scope, owner, source_folder)"
        )
        # One physical file (one blob_id) has exactly one current logical
        # description — without this, re-running the backfill/reconcile
        # scanner over an already-indexed tree creates duplicate entries
        # rows for the same file instead of updating the existing one.
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_entries_blob_unique ON entries(blob_id)"
        )
        conn.commit()


async def init_db() -> None:
    """Initialise the media index. Call once from main.py lifespan, same as document_index."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _close_pool)
    await loop.run_in_executor(None, _init_db_sync)


async def close_db() -> None:
    """Close all pooled connections. Call from main.py lifespan shutdown."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _close_pool)


# ---------------------------------------------------------------------------
# Recording — called by ingest() and by the Telegram storage functions
# ---------------------------------------------------------------------------

def _record_sync(
    *,
    rel_path: str,
    content_hash: str,
    size_bytes: int,
    scope: str,
    owner: Optional[str],
    category: str,
    media_type: Optional[str],
    source: str,
    source_folder: Optional[str],
    filename: str,
    original_name: str,
    capture_date: Optional[float],
    mtime: Optional[float],
    duration: Optional[float] = None,
) -> None:
    now = int(time.time())
    capture_date_int = int(capture_date) if capture_date else None
    mtime_int = int(mtime) if mtime else None
    with _get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO blobs (content_hash, rel_path, size_bytes, mtime) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(rel_path) DO UPDATE SET content_hash=excluded.content_hash, "
            "size_bytes=excluded.size_bytes, mtime=excluded.mtime RETURNING id",
            (content_hash, rel_path, size_bytes, mtime_int),
        )
        blob_id = cur.fetchone()[0]
        conn.execute(
            """
            INSERT INTO entries (
                blob_id, scope, owner, category, media_type, source, source_folder,
                filename, original_name, capture_date, ingest_date, duration
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(blob_id) DO UPDATE SET
                scope=excluded.scope, owner=excluded.owner, category=excluded.category,
                media_type=excluded.media_type, source=excluded.source,
                source_folder=excluded.source_folder, filename=excluded.filename,
                original_name=excluded.original_name, capture_date=excluded.capture_date,
                duration=excluded.duration,
                deleted=0
            """,
            (
                blob_id, scope, owner, category, media_type, source, source_folder,
                filename, original_name, capture_date_int, now, duration,
            ),
        )
        conn.commit()


async def record_entry(
    *,
    rel_path: str,
    content_hash: str,
    size_bytes: int,
    scope: str,
    owner: Optional[str],
    category: str,
    media_type: Optional[str],
    source: str,
    source_folder: Optional[str] = None,
    filename: str,
    original_name: Optional[str] = None,
    capture_date: Optional[float] = None,
    mtime: Optional[float] = None,
    duration: Optional[float] = None,
) -> None:
    """Record one ingested file. Never raises — this index is a queryable cache
    alongside the filesystem, not the source of truth; a failure here must
    never fail (or roll back) the file write it's describing. Logs a warning
    and moves on, since the nightly reconciliation walk is the backstop for
    anything missed here.

    Since the dedup migration off the JSON ingest_hashes store, this is ALSO
    the single canonical content-hash record backing ingest's duplicate
    detection (see find_live_by_hash) — a miss here just means one duplicate
    could slip through until the reconciler records the file, never a lost
    upload."""
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: _record_sync(
                rel_path=rel_path,
                content_hash=content_hash,
                size_bytes=size_bytes,
                scope=scope,
                owner=owner,
                category=category,
                media_type=media_type,
                source=source,
                source_folder=source_folder,
                filename=filename,
                original_name=original_name or filename,
                capture_date=capture_date,
                mtime=mtime,
                duration=duration,
            ),
        )
    except Exception as exc:
        logger.warning("media_index record failed rel_path=%s error=%s", rel_path, exc)


# ---------------------------------------------------------------------------
# Dedup lookup — backs ingest's per-(scope[, owner], hash) duplicate check.
# Replaces the JSON ingest_hashes store (which was capped at 20k entries with
# oldest-eviction, silently re-admitting duplicates at real family-photo
# scale). One canonical hash record per file, invalidation-by-delete for free:
# every delete path already flips entries.deleted, which removes the row from
# this query — no second store to keep in sync.
# ---------------------------------------------------------------------------

def _find_live_by_hash_sync(scope: str, owner: Optional[str], content_hash: str) -> Optional[dict]:
    sql = (
        "SELECT b.rel_path, e.filename, e.ingest_date FROM blobs b "
        "JOIN entries e ON e.blob_id = b.id "
        "WHERE b.content_hash = ? AND e.deleted = 0 AND e.scope = ?"
    )
    params: list = [content_hash, scope]
    if scope == "personal":
        # Personal dedup is per-owner: one member's private copy must never
        # suppress (or leak the path of) another member's upload of the same
        # bytes. The old JSON store keyed on scope alone and got this wrong.
        sql += " AND e.owner = ?"
        params.append(owner or "")
    sql += " LIMIT 1"
    with _get_conn() as conn:
        row = conn.execute(sql, params).fetchone()
    if row is None:
        return None
    saved_at = datetime.fromtimestamp(row["ingest_date"], tz=timezone.utc).isoformat()
    return {"path": row["rel_path"], "filename": row["filename"], "saved_at": saved_at}


def _find_live_by_hashes_sync(scope: str, owner: Optional[str],
                              hashes: Sequence[str]) -> set:
    """
    Which of these hashes this scope already holds — one query per chunk, not one per file.

    Exists so a client can ask "which of my 664 photos do you already have?" *before* sending
    anything. The old flow transferred every byte, wrote it to disk with an fsync, hashed it, and
    only then discovered it was a duplicate and deleted it.

    Keeps the per-owner rule from [_find_live_by_hash_sync] exactly: for `personal`, a hash only
    counts as present if **this owner** has it. Answering otherwise would let one family member
    probe another's library by hash, which is a disclosure the single-item lookup was careful to
    avoid.
    """
    found: set = set()
    if not hashes:
        return found
    # SQLite's default bound-variable ceiling is 999; the scope/owner params share the budget.
    CHUNK = 400
    unique = list(dict.fromkeys(hashes))
    with _get_conn() as conn:
        for i in range(0, len(unique), CHUNK):
            part = unique[i:i + CHUNK]
            marks = ",".join("?" * len(part))
            sql = (
                f"SELECT DISTINCT b.content_hash FROM blobs b "
                f"JOIN entries e ON e.blob_id = b.id "
                f"WHERE b.content_hash IN ({marks}) AND e.deleted = 0 AND e.scope = ?"
            )
            params: list = [*part, scope]
            if scope == "personal":
                sql += " AND e.owner = ?"
                params.append(owner or "")
            found.update(r["content_hash"] for r in conn.execute(sql, params))
    return found


async def find_live_by_hashes(scope: str, owner: Optional[str],
                              hashes: Sequence[str]) -> set:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, lambda: _find_live_by_hashes_sync(scope, owner, hashes)
    )


async def find_live_by_hash(scope: str, owner: Optional[str], content_hash: str) -> Optional[dict]:
    """Return {"path", "filename", "saved_at"} for a live (non-deleted) file with
    this content hash in this scope (owner-scoped for personal), or None."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, lambda: _find_live_by_hash_sync(scope, owner, content_hash)
    )


# ---------------------------------------------------------------------------
# Bucket allocator — the structural ≤ BUCKET_LIMIT-files-per-directory
# guarantee. Single writer-serialized (BEGIN IMMEDIATE) counter per
# (bucket_key, part); every date-bucketed write path allocates through here,
# replacing the old scandir-count-then-write check that raced under
# concurrent uploads.
# ---------------------------------------------------------------------------

BUCKET_LIMIT = 500


def allocate_bucket_sync(bucket_key: str) -> str:
    """Reserve one slot in *bucket_key* (e.g. "family/Photos/2026/07") and
    return the physical leaf directory name — the key's last component for
    part 1 ("07"), or "<leaf>-b<part>" ("07-b2") once earlier parts are full.

    Always prefers the LOWEST-numbered part with room (ORDER BY part ASC), not
    just any part with room — found live (2026-07-13 device smoke test): after
    a bucket-repair pass resets a higher part's count back down (e.g. part 2
    repaired from a stress-test-inflated count back to its real, low physical
    count), that higher part still has a row in this table. Preferring the
    HIGHEST part with room (the original, buggy ORDER BY part DESC) would then
    keep allocating into that mostly-empty higher part while a lower part with
    equal or more room sits unused — scattering files across more physical
    directories than necessary, defeating the filesystem-hygiene point of
    bucketing at all. Sequential fill (always finish part 1 before part 2 is
    ever touched) is the correct invariant regardless of repair history.

    Sync on purpose: callers are executor/thread contexts (ingest wraps it in
    run_in_executor; file_sorter._sort_file already runs in an executor).
    """
    leaf = bucket_key.rsplit("/", 1)[-1]
    with _get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT part FROM buckets WHERE bucket_key = ? AND count < ? "
                "ORDER BY part ASC LIMIT 1",
                (bucket_key, BUCKET_LIMIT),
            ).fetchone()
            if row is not None:
                part = row["part"]
                conn.execute(
                    "UPDATE buckets SET count = count + 1 WHERE bucket_key = ? AND part = ?",
                    (bucket_key, part),
                )
            else:
                max_row = conn.execute(
                    "SELECT COALESCE(MAX(part), 0) AS max_part FROM buckets WHERE bucket_key = ?",
                    (bucket_key,),
                ).fetchone()
                part = max_row["max_part"] + 1
                conn.execute(
                    "INSERT INTO buckets (bucket_key, part, count) VALUES (?, ?, 1)",
                    (bucket_key, part),
                )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    return leaf if part == 1 else f"{leaf}-b{part}"


# ---------------------------------------------------------------------------
# Querying — backs the Android Folders/timeline views. Callers ask by logical
# identity (scope, owner, source_folder, category) and never learn the
# physical rel_path directly; they get an opaque entry id instead (see
# get_entry, used by the /media/{id}/content route to resolve to bytes).
# ---------------------------------------------------------------------------

def _query_entries_sync(
    *,
    scope: str,
    owner: Optional[str],
    source_folder: Optional[str],
    category: Optional[str],
    media_type: Optional[str] = None,
    sort_by: str = "modified",
    sort_dir: str = "desc",
    before_value: Optional[int | str] = None,
    before_id: Optional[int] = None,
    limit: int,
) -> list[sqlite3.Row]:
    clauses = ["e.scope = ?", "e.deleted = 0"]
    params: list = [scope]
    if owner is not None:
        clauses.append("e.owner = ?")
        params.append(owner)
    if source_folder is not None:
        clauses.append("e.source_folder = ?")
        params.append(source_folder)
    if category is not None:
        clauses.append("e.category = ?")
        params.append(category)
    if media_type == "photo":
        clauses.append("e.media_type LIKE 'image/%'")
    elif media_type == "video":
        clauses.append("e.media_type LIKE 'video/%'")
    sort_col = "e.capture_date" if sort_by == "modified" else "e.filename"
    sql_dir = "DESC" if sort_dir == "desc" else "ASC"
    cmp_op = "<" if sort_dir == "desc" else ">"
    if before_value is not None and before_id is not None:
        clauses.append(f"({sort_col} {cmp_op} ? OR ({sort_col} = ? AND e.id {cmp_op} ?))")
        params.extend([before_value, before_value, before_id])

    where = " AND ".join(clauses)
    sql = (
        "SELECT e.id, e.scope, e.owner, e.category, e.media_type, e.source, "
        "e.source_folder, e.filename, e.original_name, e.capture_date, e.duration, b.size_bytes "
        "FROM entries e JOIN blobs b ON b.id = e.blob_id "
        f"WHERE {where} "
        f"ORDER BY {sort_col} {sql_dir}, e.id {sql_dir} LIMIT ?"
    )
    params.append(limit)
    with _get_conn() as conn:
        return conn.execute(sql, params).fetchall()


async def query_entries(
    *,
    scope: str,
    owner: Optional[str] = None,
    source_folder: Optional[str] = None,
    category: Optional[str] = None,
    media_type: Optional[str] = None,
    sort_by: str = "modified",
    sort_dir: str = "desc",
    before_value: Optional[int | str] = None,
    before_id: Optional[int] = None,
    limit: int = 60,
) -> list[dict]:
    """Keyset-paginated query — never OFFSET, which degrades badly once a
    scope holds thousands of rows."""
    loop = asyncio.get_running_loop()
    rows = await loop.run_in_executor(
        None,
        lambda: _query_entries_sync(
            scope=scope, owner=owner, source_folder=source_folder, category=category,
            media_type=media_type, sort_by=sort_by, sort_dir=sort_dir,
            before_value=before_value, before_id=before_id, limit=limit,
        ),
    )
    return [dict(r) for r in rows]


def _get_entry_sync(entry_id: int) -> Optional[dict]:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT e.id, e.scope, e.owner, e.media_type, e.filename, b.rel_path "
            "FROM entries e JOIN blobs b ON b.id = e.blob_id "
            "WHERE e.id = ? AND e.deleted = 0",
            (entry_id,),
        ).fetchone()
        return dict(row) if row else None


async def get_entry(entry_id: int) -> Optional[dict]:
    """Resolve an opaque entry id to its scope/owner (for auth) and rel_path
    (for serving bytes) — the one place a caller is allowed to see rel_path."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _get_entry_sync(entry_id))


def _mark_entry_deleted_sync(entry_id: int) -> None:
    with _get_conn() as conn:
        conn.execute("UPDATE entries SET deleted = 1 WHERE id = ?", (entry_id,))
        conn.commit()


async def mark_entry_deleted(entry_id: int) -> None:
    """Mark a single entry deleted immediately — used by the entry-id delete
    route (DELETE /api/v1/media/{id}) so a deleted item disappears from
    GET /media right away, instead of waiting for the next reconcile pass."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: _mark_entry_deleted_sync(entry_id))


def _mark_deleted_by_rel_path_sync(rel_path: str) -> int:
    with _get_conn() as conn:
        cur = conn.execute(
            "UPDATE entries SET deleted = 1 WHERE deleted = 0 AND blob_id IN "
            "(SELECT id FROM blobs WHERE rel_path = ?)",
            (rel_path,),
        )
        conn.commit()
        return cur.rowcount


async def mark_entry_deleted_by_rel_path(rel_path: str) -> int:
    """Mark any entry backed by this exact rel_path deleted immediately — used by the
    path-based /files/delete route (file_routes._soft_delete_resolved), which has no
    entry id to call mark_entry_deleted() with. Without this, a path-based delete left
    media_index unaware the file was gone until the next reconcile pass. Returns the
    number of rows affected (0 or 1 in practice, since rel_path is unique on blobs)."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _mark_deleted_by_rel_path_sync(rel_path))


def path_is_under_prefix(path: str, prefix: str) -> bool:
    """Canonical "does path belong under this prefix" rule (exact match, or a true
    subdirectory — a slash-bounded child, never a bare string prefix like "alice2" matching
    "alice"). Used on every profile delete (auth_routes.delete_my_profile,
    _mark_deleted_by_prefix_sync below) — since the dedup migration off the JSON
    ingest_hashes store, media_index is the only store this rule needs to stay consistent
    within (dedup now reads live media_index entries directly, so pruning here is sufficient
    on its own).

    _mark_deleted_by_prefix_sync deliberately does NOT reimplement this as a second SQL LIKE
    predicate: SQLite's LIKE is case-insensitive by default (a real divergence caught by
    test_prefix_matching_equivalence.py — "/personal/alicE/" matched a LIKE prefix for
    "/personal/alice" that this Python predicate correctly rejects), and any other future
    edge case would silently re-diverge the same way. Instead it does a broad, case-insensitive
    LIKE scan to cheaply narrow candidates on an indexed column, then re-checks every candidate
    against *this exact function* before deleting — so the LIKE fast-path and the exact rule
    can never disagree on which rows actually get pruned, no matter how this rule's edge cases
    evolve."""
    return path == prefix or path.startswith(f"{prefix}/")


def _like_prefix(prefix: str) -> str:
    """Broad, case-insensitive SQL LIKE candidate filter — deliberately over-inclusive (matches
    more than path_is_under_prefix would allow) since _mark_deleted_by_prefix_sync re-checks
    every candidate against path_is_under_prefix before deleting. This only needs to never
    UNDER-match (never exclude a row path_is_under_prefix would accept)."""
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{escaped}/%"


def _mark_deleted_by_prefix_sync(rel_path_prefix: str) -> int:
    with _get_conn() as conn:
        candidates = conn.execute(
            "SELECT e.id, b.rel_path FROM entries e JOIN blobs b ON b.id = e.blob_id "
            "WHERE e.deleted = 0 AND (b.rel_path = ? OR b.rel_path LIKE ? ESCAPE '\\')",
            (rel_path_prefix, _like_prefix(rel_path_prefix)),
        ).fetchall()
        matched_ids = [
            row["id"] for row in candidates
            if path_is_under_prefix(row["rel_path"], rel_path_prefix)
        ]
        if not matched_ids:
            return 0
        conn.executemany(
            "UPDATE entries SET deleted = 1 WHERE id = ?",
            [(i,) for i in matched_ids],
        )
        conn.commit()
        return len(matched_ids)


def _all_blob_keys_sync() -> dict[str, str]:
    with _get_conn() as conn:
        rows = conn.execute("SELECT rel_path, content_hash FROM blobs").fetchall()
    return {r["rel_path"]: r["content_hash"] for r in rows}


async def all_blob_keys() -> dict[str, str]:
    """
    Every indexed file as `{rel_path: content_hash}` — the shape the semantic indexer wants.

    The content hash doubles as the staleness key: a file whose bytes changed gets a new hash and
    is therefore re-embedded, while a rename keeps its hash and is re-embedded because the *path*
    is what gets embedded. Both are correct, and neither needs a separate mtime comparison.
    """
    return await asyncio.to_thread(_all_blob_keys_sync)


async def mark_entries_deleted_by_prefix(rel_path_prefix: str) -> int:
    """Mark every entry rooted under this rel_path prefix deleted immediately — used when a
    whole directory (e.g. a deleted user's personal folder) is removed in one shot via
    shutil.rmtree, which has no per-file rel_path to call mark_entry_deleted_by_rel_path()
    with for each one. Returns the number of rows affected."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _mark_deleted_by_prefix_sync(rel_path_prefix))


def _rename_owner_sync(old_name: str, new_name: str) -> int:
    with _get_conn() as conn:
        cur = conn.execute(
            "UPDATE entries SET owner = ? WHERE scope = 'personal' AND owner = ?",
            (new_name, old_name),
        )
        conn.commit()
        return cur.rowcount


async def rename_owner(old_name: str, new_name: str) -> int:
    """Repoint every personal-scope entry's owner from old_name to new_name — called when a
    profile is renamed (auth_routes.update_my_profile). /media queries authorize and filter
    purely on this owner column, never on the physical folder name (files are resolved via
    their stored rel_path, immutable regardless of the current display name), so this alone is
    enough to keep a member's pre-rename personal photos visible under scope=personal&owner=
    <newname> — without it they'd silently stop matching and "vanish" from Mine. Returns the
    number of rows migrated."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _rename_owner_sync(old_name, new_name))


def _mark_missing_deleted_sync(nas_root: Path) -> int:
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT e.id, b.rel_path FROM entries e JOIN blobs b ON b.id = e.blob_id "
            "WHERE e.deleted = 0"
        ).fetchall()
        missing_ids = [
            row["id"] for row in rows
            if not (nas_root / row["rel_path"].lstrip("/")).is_file()
        ]
        if missing_ids:
            conn.executemany(
                "UPDATE entries SET deleted = 1 WHERE id = ?",
                [(i,) for i in missing_ids],
            )
            conn.commit()
        return len(missing_ids)


async def mark_missing_deleted(nas_root: Path) -> int:
    """Reconcile pass: mark entries whose backing file no longer exists on
    disk as deleted. Covers two real gaps — a file removed/moved directly
    over the raw SMB share (media_index can't see that happen at all), and
    /files/delete's soft-delete-to-trash, which moves the file but doesn't
    yet update media_index. Only ever flips 0 -> 1; safe to re-run anytime."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _mark_missing_deleted_sync(nas_root))


# ---------------------------------------------------------------------------
# Reconciler support — backs app/media_reconciler.py's nightly pass. Two
# things the reconciler needs that nothing else in this module provides:
# a cheap way to skip re-hashing unchanged files, and a way to repair the
# bucket allocator's counters against ground truth.
# ---------------------------------------------------------------------------

def _existing_blob_signatures_sync() -> dict[str, tuple[int, Optional[int]]]:
    with _get_conn() as conn:
        rows = conn.execute("SELECT rel_path, size_bytes, mtime FROM blobs").fetchall()
    return {row["rel_path"]: (row["size_bytes"], row["mtime"]) for row in rows}


async def existing_blob_signatures() -> dict[str, tuple[int, Optional[int]]]:
    """rel_path -> (size_bytes, mtime) for every blob currently recorded, live or
    soft-deleted. The reconciler's incremental scan skips re-hashing (SHA-256 over
    a whole family photo library is hours of CPU+I/O on a 1GB-RAM ARM board — not
    viable to do nightly) any file whose (rel_path, size, mtime) already matches a
    row here; only new or genuinely changed files get re-hashed and re-recorded."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _existing_blob_signatures_sync)


def _repair_bucket_counts_sync(nas_root: Path) -> int:
    """Recompute every bucket's `count` from a real directory scan and correct any
    drift — the allocator's counter can only drift from ground truth via a crash
    between allocation and file write, or an out-of-band file dropped in over SMB
    outside the allocator entirely; this is the backstop that heals it, since the
    allocator itself only ever increments, never re-checks reality."""
    repaired = 0
    with _get_conn() as conn:
        rows = conn.execute("SELECT bucket_key, part, count FROM buckets").fetchall()
        for row in rows:
            bucket_key, part = row["bucket_key"], row["part"]
            leaf = bucket_key.rsplit("/", 1)[-1]
            dir_name = leaf if part == 1 else f"{leaf}-b{part}"
            parent = "/".join(bucket_key.split("/")[:-1])
            bucket_dir = nas_root / parent / dir_name
            real_count = 0
            try:
                with _os_scandir_files(bucket_dir) as it:
                    for _ in it:
                        real_count += 1
            except OSError:
                real_count = 0
            if real_count != row["count"]:
                conn.execute(
                    "UPDATE buckets SET count = ? WHERE bucket_key = ? AND part = ?",
                    (real_count, bucket_key, part),
                )
                repaired += 1
                if real_count > BUCKET_LIMIT:
                    logger.warning(
                        "bucket_over_limit key=%s part=%s count=%d limit=%d — "
                        "needs an explicit admin rebucket pass, not auto-moved",
                        bucket_key, part, real_count, BUCKET_LIMIT,
                    )
        conn.commit()
    return repaired


@contextmanager
def _os_scandir_files(path: Path):
    """os.scandir wrapped as a context manager yielding only file entries — streams
    the directory rather than materializing a full listing, since a bucket at the
    500-file limit is small but this same helper pattern must never be copied onto
    an unbounded directory (e.g. a not-yet-rebucketed legacy Backups/<id>/ dump)."""
    scandir_it = os.scandir(path)
    try:
        yield (e for e in scandir_it if e.is_file())
    finally:
        scandir_it.close()


async def repair_bucket_counts(nas_root: Path) -> int:
    """Reconcile pass: correct every bucket allocator counter to match a real
    directory scan. Returns the number of (bucket_key, part) rows corrected."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _repair_bucket_counts_sync(nas_root))


# ---------------------------------------------------------------------------
# Duration backfill
#
# `duration` was added to `entries` as a later migration (see _init_db_sync), so every video
# indexed before it exists with duration NULL — and the reconciler only revisits a file whose
# (size, mtime) signature has CHANGED, which for a film sitting untouched on a drive is never.
# 334 of 334 videos on the production board were NULL: not a sampling gap, a structural one.
#
# Any column added to this table in future inherits the same blind spot, so the shape here —
# "find rows the current code could fill but did not, and fill them out of band" — is worth
# reusing rather than treating as a one-off script.
#
# `scripts/backfill_media_index.py` already exists and would also fix this, by re-walking the NAS
# and re-recording every file. It is the wrong tool here: it re-hashes every file it visits, which
# on this board means sha256 over hundreds of GB including multi-GB films, to populate one column
# on rows that already exist — and it has to be run by hand over SSH. It stays the right answer for
# what it was written for (files with no DB row at all) and is the fallback if this ever diverges.
# ---------------------------------------------------------------------------

def _videos_missing_duration_sync(limit: int) -> list[tuple[int, str]]:
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT e.id AS id, b.rel_path AS rel_path FROM entries e "
            "JOIN blobs b ON b.id = e.blob_id "
            "WHERE e.deleted = 0 AND e.media_type LIKE 'video/%' AND e.duration IS NULL "
            "ORDER BY e.id LIMIT ?",
            (limit,),
        ).fetchall()
    return [(r["id"], r["rel_path"]) for r in rows]


async def videos_missing_duration(limit: int = 200) -> list[tuple[int, str]]:
    """Indexed videos whose duration was never probed."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _videos_missing_duration_sync(limit))


def _set_duration_sync(entry_id: int, duration: float) -> None:
    with _get_conn() as conn:
        conn.execute("UPDATE entries SET duration = ? WHERE id = ?", (duration, entry_id))
        conn.commit()


async def set_duration(entry_id: int, duration: float) -> None:
    """
    Record a probed duration. **0.0 means "probed, and there is no answer".**

    Leaving a failed probe as NULL would put the file back in the queue on every restart, and a
    file ffprobe cannot read today it cannot read tomorrow either — so the backfill would re-probe
    the same broken files forever. Clients already treat a falsy duration as absent, which is the
    same thing they did with NULL, so nothing downstream changes.
    """
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: _set_duration_sync(entry_id, duration))

# ---------------------------------------------------------------------------
def _entry_ids_for_paths_sync(rel_paths: list[str]) -> dict[str, int]:
    """
    Map NAS-relative paths to media-index entry ids, in one query per chunk.

    Exists so a directory listing can hand the client a stable identity for each file without a
    round trip per item. Chunked because SQLite caps host parameters (999 by default) and a listing
    page can legitimately be 500 entries.

    Only live entries: a deleted one must not resurrect a resume point.
    """
    if not rel_paths:
        return {}
    found: dict[str, int] = {}
    CHUNK = 400
    with _get_conn() as conn:
        for i in range(0, len(rel_paths), CHUNK):
            part = rel_paths[i:i + CHUNK]
            marks = ",".join("?" * len(part))
            rows = conn.execute(
                f"SELECT b.rel_path, e.id FROM entries e JOIN blobs b ON b.id = e.blob_id "
                f"WHERE e.deleted = 0 AND b.rel_path IN ({marks})",
                tuple(part),
            ).fetchall()
            for r in rows:
                found[r["rel_path"]] = r["id"]
    return found


async def entry_ids_for_paths(rel_paths: list[str]) -> dict[str, int]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _entry_ids_for_paths_sync(rel_paths))


# ---------------------------------------------------------------------------
# Playback positions — resume where you left off, on any device
# ---------------------------------------------------------------------------

#: Below this, treat it as "never really started" and forget it rather than offering to resume the
#: first few seconds of something. Above [_DONE_FRACTION] it counts as watched and is cleared, so a
#: finished film does not sit in Continue Watching forever asking to be resumed at the credits.
_MIN_RESUME_SECONDS = 30.0
_DONE_FRACTION = 0.95


#: Client timestamps are epoch milliseconds. Clamped rather than rejected: a device with a wildly
#: wrong clock must not be able to put itself into a state where every future sync 4xx's forever.
#: Clamping keeps it participating (and losing conflicts, which is the right outcome) without ever
#: raising, overflowing, or wedging synchronisation.
_MAX_CLIENT_TS_MS = 4102444800000  # 2100-01-01T00:00:00Z


def _sane_client_ts(value: object) -> int:
    """Coerce a client-supplied timestamp into a usable epoch-ms integer. Never raises."""
    try:
        ts = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return 0
    if ts < 0:
        return 0
    return min(ts, _MAX_CLIENT_TS_MS)


def _set_position_sync(username: str, entry_id: int, position: float, duration: float | None,
                       client_updated_at: object = 0) -> dict:
    """
    Apply a position report, resolving conflicts by CLIENT timestamp.

    Returns {"version": int, "cleared": bool, "applied": bool}.

    Conflict rule, enforced here on the server so a stale device cannot clobber a newer one no
    matter how the client behaves:

      * no stored row            -> apply
      * incoming ts >  stored ts -> apply, version += 1
      * incoming ts == stored ts -> DO NOT apply. Equal is not newer. This is also what makes a
                                    retried request idempotent: replaying the same report returns
                                    the same version and changes nothing.
      * incoming ts <  stored ts -> DO NOT apply. A device that was offline for days must not
                                    overwrite what another device did yesterday.

    Clearing (below _MIN_RESUME_SECONDS, or at/above _DONE_FRACTION) writes a **tombstone** —
    position 0, timestamp retained — rather than deleting the row. Deleting would lose the
    timestamp, and then a stale in-flight report arriving after a completion would find no row,
    count as "no stored state", and resurrect a resume point the user had already finished.
    `positions()` filters tombstones out, so nothing changes for callers.
    """
    incoming_ts = _sane_client_ts(client_updated_at)
    finished = bool(duration and duration > 0 and position >= duration * _DONE_FRACTION)
    should_clear = position < _MIN_RESUME_SECONDS or finished

    with _get_conn() as conn:
        row = conn.execute(
            "SELECT position, version, client_updated_at FROM playback_positions "
            "WHERE username = ? AND entry_id = ?",
            (username, entry_id),
        ).fetchone()

        if row is not None and incoming_ts <= row["client_updated_at"]:
            return {"version": row["version"], "cleared": row["position"] <= 0, "applied": False}

        version = (row["version"] + 1) if row is not None else 1
        stored_position = 0.0 if should_clear else float(position)
        conn.execute(
            "INSERT INTO playback_positions "
            "  (username, entry_id, position, duration, updated_at, version, client_updated_at) "
            "VALUES (?, ?, ?, ?, strftime('%s','now'), ?, ?) "
            "ON CONFLICT(username, entry_id) DO UPDATE SET "
            "  position=excluded.position, duration=excluded.duration, "
            "  updated_at=excluded.updated_at, version=excluded.version, "
            "  client_updated_at=excluded.client_updated_at",
            (username, entry_id, stored_position, duration, version, incoming_ts),
        )
        conn.commit()
        return {"version": version, "cleared": should_clear, "applied": True}


async def set_position(username: str, entry_id: int, position: float, duration: float | None,
                       client_updated_at: object = 0) -> dict:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        lambda: _set_position_sync(username, entry_id, position, duration, client_updated_at),
    )


def _positions_sync(username: str, limit: int) -> list[dict]:
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT p.entry_id, p.position, p.duration, p.updated_at, p.version, "
            "       p.client_updated_at "
            "FROM playback_positions p JOIN entries e ON e.id = p.entry_id "
            # position > 0 skips tombstones — rows kept only so a stale report cannot resurrect a
            # finished item. Ordered by the CLIENT timestamp: p.updated_at is this board's clock.
            "WHERE p.username = ? AND e.deleted = 0 AND p.position > 0 "
            "ORDER BY p.client_updated_at DESC, p.updated_at DESC LIMIT ?",
            (username, limit),
        ).fetchall()
    # Joined against entries so a film deleted from the library stops being offered for resume.
    return [
        {
            "entryId": r["entry_id"],
            "position": r["position"],
            "duration": r["duration"],
            "updatedAt": r["updated_at"],
            "version": r["version"],
            "clientUpdatedAt": r["client_updated_at"],
        }
        for r in rows
    ]


async def positions(username: str, limit: int = 100) -> list[dict]:
    """Everything this person has part-watched, most recent first."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _positions_sync(username, limit))
