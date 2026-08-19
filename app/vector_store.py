"""
Storage and search for semantic-search vectors (Phase 4 milestone 2).

Deliberately knows nothing about embeddings. It stores vectors someone else produced and finds
the nearest ones — so the model can be swapped, or the whole thing tested, without an inference
runtime present. `numpy` is already a transitive dependency of the deployed backend; nothing new
is added by this module.

## Why float32 by default

Measured on the Cubie A5E — the smallest board in the fleet, 937 MB RAM, backend running
(`docs/PHASE4_EMBEDDING_SPIKE_2026-08-05.md`):

    float32, direct dot           148.5 MB / 100k    28.0 ms
    int8, upcast to int16          36.9 MB / 100k   280.5 ms
    int8, upcast to float32        36.6 MB / 100k   177.9 ms
    int8, blocked float32          36.6 MB / 100k    66.7 ms

int8 quarters the memory and costs an order of magnitude in latency if done naively, because the
upcast dominates the dot product. Blocking recovers most of it — but 66.7 ms of search plus
~22.6 ms of query embedding is 89 ms, past the 75 ms budget, where float32 lands near 50 ms.

So float32 is the default and int8 is the pressure valve, chosen when memory matters more than
latency. An earlier draft of the plan mandated int8; that generalised an out-of-memory failure
which was actually caused by holding two indices and a heavier model at once.

## Durability posture

The vectors are a **derived cache and nothing else**. Losing this database loses no user data and
no access to any file — it costs a rescan. That is why they live in their own file rather than
alongside anything authoritative, and why [rebuild_required] exists: it reports what a rescan
would need to recompute, without pretending the store can repair itself.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import queue
import re
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Generator, Iterable, Sequence

import numpy as np

from . import vector_index
from .config import settings

logger = logging.getLogger("aihomecloud.vector_store")

DIM = 384
"""Default dimension, kept for callers that predate per-model support.

**The width is a property of the model, not of this module** — bge-small is 384, base is 768,
large is 1024. Every write records its own `dim` and every read groups by model, because a vector
of the wrong width does not raise; it silently produces meaningless similarity scores, which is
the worst way for this to fail. `expected_dim` on the write path is what turns that into an
error."""

_SEARCH_BLOCK_ROWS = 10_000
"""Rows per block when searching an int8 index. Measured: blocking at this size takes int8 search
from 178 ms to 67 ms at 100k on the Cubie, by keeping each upcast inside cache."""


@dataclass(frozen=True)
class Hit:
    rel_path: str
    score: float


SHARED_SHARD = "__shared__"
"""The family/shared library, searched alongside whichever user is asking."""

# Dots are NOT permitted. An earlier version allowed them, so "../../etc/passwd" sanitised to
# ".._.._etc_passwd" — still carrying "..", still a traversal attempt, and it looked sanitised.
# Profile names have no need for dots, so the safe set simply excludes them.
_SAFE_SHARD = re.compile(r"[^A-Za-z0-9_-]")


def shard_for_path(rel_path: str) -> str:
    """
    Which shard a file belongs to, from its path.

    `<nas_root>/personal/<user>/...` is that user's own library; everything else — family,
    entertainment, anything shared — lands in the shared shard. This is the routing rule the
    indexer uses, and it mirrors how the app already scopes browsing, so a file's shard and its
    visibility never disagree.
    """
    root = str(getattr(settings, "nas_root", "")).strip("/")
    personal = str(getattr(settings, "personal_base", "personal")).strip("/")
    parts = [p for p in rel_path.strip("/").split("/") if p]
    if root:
        root_parts = [p for p in root.split("/") if p]
        if parts[:len(root_parts)] == root_parts:
            parts = parts[len(root_parts):]
    if len(parts) >= 2 and parts[0] == personal:
        return shard_key(parts[1])
    return SHARED_SHARD


def shards_for_user(username: str | None) -> list[str]:
    """
    The shards a search should cover: the caller's own, plus shared.

    Two shards, not five. Measured on the production board, that is 11 ms + 11 ms rather than
    65 ms over one combined index — and it is also the correct privacy boundary, since a member
    should not be searching another member's personal library.
    """
    own = shard_key(username)
    return [own, SHARED_SHARD] if own != SHARED_SHARD else [SHARED_SHARD]


def search_shards(
    query: Sequence[float] | np.ndarray, *, model: str, shards: Sequence[str], limit: int = 20
) -> list[Hit]:
    """
    Search several shards and merge, best first.

    Scores are comparable across shards because every shard uses the same model — which is why
    per-shard model choice is deliberately not offered, however tempting it looks.
    """
    merged: list[Hit] = []
    for shard in dict.fromkeys(shards):          # de-duplicated, order preserved
        merged.extend(search(query, model=model, limit=limit, shard=shard))
    merged.sort(key=lambda h: h.score, reverse=True)
    return merged[:limit]


def shard_key(name: str | None) -> str:
    """
    A filesystem-safe shard name.

    Sanitised because it becomes a filename: a profile called `../../etc` must not choose where
    the database lands. Empty or unknown resolves to the shared shard, so a caller that has not
    resolved an identity still searches something sensible rather than nothing.
    """
    if not name:
        return SHARED_SHARD
    raw = name.strip()
    cleaned = _SAFE_SHARD.sub("_", raw)[:64]
    if not cleaned:
        return SHARED_SHARD
    if cleaned == raw:
        return cleaned
    # Sanitising is lossy, and two different members can sanitise to the same string —
    # `Mr.Paras` and `Mr_Paras` both become `Mr_Paras`. That would put two people's vectors in
    # one file and merge their search results, which is the exact privacy boundary sharding
    # exists to enforce. A short digest of the ORIGINAL name keeps them apart. Names that need
    # no sanitising are returned untouched, so existing shard files keep their names and no
    # migration is needed for them.
    digest = hashlib.blake2s(raw.encode("utf-8"), digest_size=4).hexdigest()
    return f"{cleaned[:55]}-{digest}"


def _db_path(shard: str = SHARED_SHARD) -> Path:
    """
    One SQLite file per shard.

    **Why sharded at all** — measured on the production board (Cubie A5E, 937 MB, 8 cores):

        40k vectors    11 ms      one user's library
        100k           27 ms
        200k           65 ms      tight against the ~52 ms search budget
        400k           OOM

    and concurrency is worse than the medians suggest: 15 simultaneous searches over a single
    100k index give **194 ms median, 372 ms max**, because numpy's BLAS is already multi-threaded
    and parallel searches contend for the same cores. A family NAS has several people searching at
    once by definition.

    At 10-40k per shard an exact scan is 11 ms, which keeps concurrent use inside budget and
    removes approximate search from the common path entirely — no clustering, no recall loss, no
    index to rebuild or go stale. Sharding is the thing that makes the simple path viable.
    """
    return settings.data_dir / "vectors" / f"{shard}.db"


# --- connection pools, one per shard ----------------------------------------

_POOL_SIZE = 2
_pools: dict[str, "queue.Queue[sqlite3.Connection]"] = {}
_pools_lock = threading.Lock()


def _pool_for(shard: str) -> "queue.Queue[sqlite3.Connection]":
    with _pools_lock:
        pool = _pools.get(shard)
        if pool is None:
            pool = queue.Queue(maxsize=_POOL_SIZE)
            _pools[shard] = pool
        return pool


def _new_conn(shard: str) -> sqlite3.Connection:
    path = _db_path(shard)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    # Schema created on first connection to a shard rather than by a startup call. Shards appear
    # when a family member does — there is no moment at boot when the full set is known, and
    # "call init_db for every user first" is the kind of requirement that gets forgotten and then
    # fails as `no such table` at the worst moment.
    _create_schema(conn)
    return conn


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS vectors (
            rel_path    TEXT NOT NULL,
            dim         INTEGER NOT NULL,
            dtype       TEXT NOT NULL,
            vector      BLOB NOT NULL,
            model       TEXT NOT NULL,
            content_key TEXT NOT NULL,
            PRIMARY KEY (rel_path, model)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vectors_content ON vectors(content_key)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vectors_model ON vectors(model)")
    conn.commit()
    _widen_primary_key(conn)


def _widen_primary_key(conn: sqlite3.Connection) -> None:
    """
    Migrate `PRIMARY KEY (rel_path)` to `PRIMARY KEY (rel_path, model)`.

    The original key allowed **one vector per file**, which was correct while there was one model.
    A second model — image embeddings, whose vectors describe the same file from a different space
    — makes it actively dangerous: `INSERT OR REPLACE` would quietly *overwrite* a file's text
    vector with its image vector. Not an error, not a conflict; the text vector simply disappears
    and search silently gets worse.

    SQLite cannot alter a primary key in place, so the table is rebuilt. Every other query in this
    module already filters by `model`, so nothing else changes.
    """
    cols = conn.execute("PRAGMA table_info(vectors)").fetchall()
    if not cols:
        return
    pk_cols = sorted((c["pk"], c["name"]) for c in cols if c["pk"])
    if [name for _, name in pk_cols] == ["rel_path", "model"]:
        return  # already migrated
    if [name for _, name in pk_cols] != ["rel_path"]:
        return  # unrecognised shape — leave it alone rather than guess

    logger.info("vector_schema_widening_primary_key")
    conn.executescript(
        """
        BEGIN IMMEDIATE;
        CREATE TABLE IF NOT EXISTS vectors_new (
            rel_path    TEXT NOT NULL,
            dim         INTEGER NOT NULL,
            dtype       TEXT NOT NULL,
            vector      BLOB NOT NULL,
            model       TEXT NOT NULL,
            content_key TEXT NOT NULL,
            PRIMARY KEY (rel_path, model)
        );
        INSERT OR REPLACE INTO vectors_new
            SELECT rel_path, dim, dtype, vector, model, content_key FROM vectors;
        DROP TABLE vectors;
        ALTER TABLE vectors_new RENAME TO vectors;
        CREATE INDEX IF NOT EXISTS idx_vectors_content ON vectors(content_key);
        CREATE INDEX IF NOT EXISTS idx_vectors_model ON vectors(model);
        COMMIT;
        """
    )


@contextlib.contextmanager
def _get_conn(shard: str = SHARED_SHARD) -> Generator[sqlite3.Connection, None, None]:
    pool = _pool_for(shard)
    try:
        conn = pool.get_nowait()
    except queue.Empty:
        conn = _new_conn(shard)
    try:
        yield conn
    finally:
        try:
            pool.put_nowait(conn)
        except queue.Full:
            conn.close()


def init_db(shard: str = SHARED_SHARD) -> None:
    with _get_conn(shard) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS vectors (
                rel_path    TEXT PRIMARY KEY,
                dim         INTEGER NOT NULL,
                dtype       TEXT NOT NULL,
                vector      BLOB NOT NULL,
                model       TEXT NOT NULL,
                content_key TEXT NOT NULL
            )
            """
        )
        # `content_key` is whatever the caller uses to decide staleness — a hash, or
        # size+mtime. Indexed because the rescan path asks "which of these changed?" for
        # every file in the library, and a table scan per file is what makes rescans slow.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_vectors_content ON vectors(content_key)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_vectors_model ON vectors(model)")
        conn.commit()


def migrate_legacy_db() -> dict:
    """
    Move a pre-sharding `vectors.db` into the sharded layout, once.

    Sharding changed where vectors live — `vectors.db` became `vectors/<shard>.db`. Nothing reads
    the old file any more, so a board that upgrades keeps a perfectly good index on disk and
    answers every semantic search with **zero results and no error**. That is the worst failure
    shape available: silent, and indistinguishable from "nothing matched". It was caught by hand
    on the three boards here; anyone else upgrading would just find search quietly broken until
    the next full library scan.

    Rows are routed through `shard_for_path`, the same function the live path uses, so a migrated
    index lands exactly where a freshly built one would. The legacy file is renamed rather than
    deleted — it is the rollback, and it costs a few hundred KB.

    Safe to call on every startup: it returns immediately once the marker exists.
    """
    legacy = settings.data_dir / "vectors.db"
    if not legacy.exists():
        return {"migrated": 0, "reason": "no legacy db"}

    try:
        src = sqlite3.connect(f"file:{legacy}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        logger.warning("vector_legacy_migrate_open_failed error=%s", exc)
        return {"migrated": 0, "reason": "unreadable"}

    moved: dict[str, int] = {}
    try:
        try:
            rows = src.execute(
                "SELECT rel_path, dim, dtype, vector, model, content_key FROM vectors"
            ).fetchall()
        except sqlite3.Error:
            # No `vectors` table — an empty file from a board that never indexed anything.
            rows = []
    finally:
        src.close()

    for row in rows:
        shard = shard_for_path(row[0])
        init_db(shard)
        with _get_conn(shard) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO vectors "
                "(rel_path, dim, dtype, vector, model, content_key) VALUES (?, ?, ?, ?, ?, ?)",
                row,
            )
            conn.commit()
        moved[shard] = moved.get(shard, 0) + 1

    _invalidate()
    retired = legacy.with_suffix(".db.pre-shard")
    try:
        legacy.rename(retired)
    except OSError as exc:  # a failed rename must not re-run the migration forever
        logger.warning("vector_legacy_retire_failed error=%s", exc)

    total = sum(moved.values())
    if total:
        logger.info("vector_legacy_migrated total=%d shards=%s", total, moved)
    return {"migrated": total, "by_shard": moved}


def close_pool() -> None:
    """Close every shard's pool. Called at shutdown."""
    with _pools_lock:
        pools = list(_pools.values())
        _pools.clear()
    for pool in pools:
        while not pool.empty():
            try:
                pool.get_nowait().close()
            except queue.Empty:
                break


# --- writing ----------------------------------------------------------------

def _normalise(vec: np.ndarray) -> np.ndarray:
    """
    Unit-length, so a dot product *is* cosine similarity.

    Normalising once at write time rather than per query is what lets search be a single matrix
    multiply. A zero vector cannot be normalised; it is passed through rather than turned into
    NaN, since NaN would poison every subsequent comparison silently.
    """
    norm = float(np.linalg.norm(vec))
    return vec if norm < 1e-9 else (vec / norm)


def upsert(
    rel_path: str,
    vector: Sequence[float] | np.ndarray,
    *,
    model: str,
    content_key: str,
    quantise: bool = False,
    expected_dim: int | None = None,
    shard: str = SHARED_SHARD,
) -> None:
    """
    Store one vector, replacing any previous one for [rel_path].

    [expected_dim] is the model's width. Passing it turns a model/vector mismatch into an
    immediate error instead of a library that searches badly for reasons nobody can find later.
    Omitting it accepts whatever width is given, which is only right when the caller has already
    validated it.

    [quantise] trades memory for latency — see the module docstring. It is per-row rather than
    global on purpose: a library can be migrated a batch at a time instead of all at once.
    """
    arr = np.asarray(vector, dtype=np.float32).reshape(-1)
    dim = int(arr.shape[0])
    if expected_dim is not None and dim != expected_dim:
        raise ValueError(
            f"expected {expected_dim} dimensions for model {model!r}, got {dim} for {rel_path!r}"
        )
    if dim <= 0:
        raise ValueError(f"empty vector for {rel_path!r}")
    arr = _normalise(arr)

    if quantise:
        # Symmetric int8: the vector is already unit-length, so components are within [-1, 1]
        # and a fixed 127 scale needs no per-row scale factor to store or reapply.
        payload = np.clip(np.rint(arr * 127.0), -127, 127).astype(np.int8).tobytes()
        dtype = "int8"
    else:
        payload = arr.tobytes()
        dtype = "float32"

    with _get_conn(shard) as conn:
        conn.execute(
            "INSERT INTO vectors (rel_path, dim, dtype, vector, model, content_key) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(rel_path, model) DO UPDATE SET "
            "dim=excluded.dim, dtype=excluded.dtype, vector=excluded.vector, "
            "model=excluded.model, content_key=excluded.content_key",
            (rel_path, dim, dtype, payload, model, content_key),
        )
        conn.commit()
    _invalidate(model, shard)


def delete(rel_paths: Iterable[str], shard: str = SHARED_SHARD) -> int:
    paths = list(rel_paths)
    if not paths:
        return 0
    with _get_conn(shard) as conn:
        cur = conn.executemany("DELETE FROM vectors WHERE rel_path = ?", [(p,) for p in paths])
        conn.commit()
    # Deletes are not model-scoped, so every resident index may now be stale.
    _invalidate(shard=shard)
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else len(paths)


# --- reading ----------------------------------------------------------------

# --- resident index ---------------------------------------------------------
#
# The index is held in memory and rebuilt only when it changes. This is not an optimisation, it
# is the difference between working and not: measured on the Cubie at 100k items, reloading from
# SQLite per query costs **2374 ms** (float32) and **1595 ms** (int8), against 28 ms and 67 ms
# once the matrix is resident. The spike that produced those smaller numbers benchmarked raw
# numpy with the matrix already in hand, and reading rows back turned out to dominate everything
# else by two orders of magnitude.
#
# The cost is that the index occupies RAM for as long as it is loaded — 148 MB per 100k float32
# vectors, 37 MB at int8. On the 937 MB board that is the whole reason the dtype choice matters.
_MAX_RESIDENT_SHARDS = 3
"""How many shard matrices stay in RAM. Three covers a user, the shared library and one more
searching at the same time; beyond that the 937 MB board is the constraint, not the convenience."""

_cache_lock = threading.Lock()
_cached: dict[tuple[str, str], tuple[list[str], np.ndarray, str]] = {}


def _invalidate(model: str | None = None, shard: str | None = None) -> None:
    """Drop the resident index. Called on every write — correctness before speed."""
    with _cache_lock:
        if model is None and shard is None:
            _cached.clear()
        elif model is None:
            for key in [k for k in _cached if k[0] == shard]:
                _cached.pop(key, None)
        else:
            _cached.pop((shard or SHARED_SHARD, model), None)


def _resident(model: str, shard: str = SHARED_SHARD) -> tuple[list[str], np.ndarray | None, str]:
    """
    The resident matrix for one shard.

    **Keyed by (shard, model), not model.** Keyed by model alone — as an earlier version was —
    the first shard loaded is returned for every shard afterwards, so one family member's search
    silently answers with another's files. That is a privacy failure that looks like working
    search, and only a test that populated two shards caught it.
    """
    key = (shard, model)
    with _cache_lock:
        hit = _cached.get(key)
    if hit is not None:
        return hit[0], hit[1], hit[2]
    paths, mat, dtype = _load_all(model, shard)
    if mat is not None:
        with _cache_lock:
            # Bounded: an unbounded cache of resident matrices is how a 937 MB board dies.
            while len(_cached) >= _MAX_RESIDENT_SHARDS:
                _cached.pop(next(iter(_cached)))
            _cached[key] = (paths, mat, dtype)
    return paths, mat, dtype


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """
    No-op now that every connection creates its schema on open.

    This used to raise "init_db() was not called", which was the right guard when schema creation
    was a separate startup step that could be — and once was — forgotten. Shards removed the need:
    a shard appears when a family member does, so there is no boot-time moment when the full set
    is known. Kept as a call site so the intent stays visible at each entry point.
    """
    return


def _load_all(model: str, shard: str = SHARED_SHARD) -> tuple[list[str], np.ndarray | None, str]:
    """Every stored vector for [model], as one matrix. Mixed dtypes are upcast to float32."""
    with _get_conn(shard) as conn:
        _ensure_schema(conn)
        rows = conn.execute(
            "SELECT rel_path, dim, dtype, vector FROM vectors WHERE model = ? ORDER BY rowid",
            (model,),
        ).fetchall()
    if not rows:
        return [], None, "float32"

    # Width comes from the rows. A store holding two widths for one model name cannot happen
    # through this module, but if it ever did, every similarity score would be meaningless — so
    # it is refused loudly rather than reshaped into something plausible.
    widths = {int(r["dim"]) for r in rows}
    if len(widths) != 1:
        raise ValueError(f"model {model!r} has vectors of mixed widths {sorted(widths)}")
    dim = widths.pop()

    paths = [r["rel_path"] for r in rows]
    dtypes = {r["dtype"] for r in rows}
    if dtypes == {"int8"}:
        mat = np.frombuffer(b"".join(r["vector"] for r in rows), dtype=np.int8)
        return paths, mat.reshape(len(rows), dim), "int8"

    # Mixed or float32. Upcasting int8 rows individually keeps a partially-migrated library
    # searchable rather than requiring the whole store to share one representation.
    out = np.empty((len(rows), dim), dtype=np.float32)
    for i, r in enumerate(rows):
        if r["dtype"] == "int8":
            out[i] = np.frombuffer(r["vector"], dtype=np.int8).astype(np.float32) / 127.0
        else:
            out[i] = np.frombuffer(r["vector"], dtype=np.float32)
    return paths, out, "float32"


def _ivf_dir(model: str, shard: str = SHARED_SHARD) -> Path:
    """One index directory per model — different widths must never share files."""
    return settings.data_dir / "ivf" / shard / model


def index_count(model: str, shard: str = SHARED_SHARD) -> int:
    with _get_conn(shard) as conn:
        _ensure_schema(conn)
        return int(conn.execute(
            "SELECT COUNT(*) FROM vectors WHERE model = ?", (model,)
        ).fetchone()[0])


def build_ann_index(model: str, shard: str = SHARED_SHARD) -> dict:
    """
    Build the approximate index for [model] from what is currently stored.

    Called after a large indexing run. Below [vector_index.IVF_MIN_ITEMS] this is a no-op: an
    exact scan of that many vectors is faster than approximating, and exact beats approximate
    when both fit the budget.
    """
    n = index_count(model, shard)
    if n < vector_index.IVF_MIN_ITEMS:
        return {"built": False, "reason": "below threshold", "count": n}

    dim = _model_dim(model, shard)
    if dim is None:
        return {"built": False, "reason": "no vectors", "count": 0}

    # Streamed from SQLite in chunks. Loading the whole matrix would need 1.5 GB at a million
    # vectors, which the smallest board cannot hold — and the build is the only step that ever
    # wanted them all at once.
    def chunks():
        return _iter_vectors(model, dim, shard=shard)

    sample = _sample_vectors(model, dim, shard=shard, target=min(n, max(200_000, 50_000)))
    idx = vector_index.IvfIndex(_ivf_dir(model, shard))
    result = idx.build_streaming(count=n, dim=dim, chunks=chunks, sample=sample)
    paths = _ordered_paths(model, shard)
    # The row->path mapping is snapshotted with the index. Search filters its hits against the
    # live table afterwards, so a file deleted since the build cannot come back as a result.
    (_ivf_dir(model, shard) / "paths.json").write_text(json.dumps(paths))
    _invalidate(model, shard)          # the exact path should not keep 1M vectors resident afterwards
    logger.info("ann_index_built model=%s %s", model, result)
    return {"built": True, **result}


def _model_dim(model: str, shard: str = SHARED_SHARD) -> int | None:
    with _get_conn(shard) as conn:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT dim FROM vectors WHERE model = ? LIMIT 1", (model,)
        ).fetchone()
    return int(row[0]) if row else None


def _ordered_paths(model: str, shard: str = SHARED_SHARD) -> list[str]:
    """Paths in the same rowid order the chunk iterator yields — the index's row ids refer to it."""
    with _get_conn(shard) as conn:
        _ensure_schema(conn)
        return [r[0] for r in conn.execute(
            "SELECT rel_path FROM vectors WHERE model = ? ORDER BY rowid", (model,)
        ).fetchall()]


def _decode(blob: bytes, dtype: str, dim: int) -> np.ndarray:
    if dtype == "int8":
        return np.frombuffer(blob, dtype=np.int8).astype(np.float32) / 127.0
    return np.frombuffer(blob, dtype=np.float32)


def _iter_vectors(model: str, dim: int, chunk: int = 20_000, shard: str = SHARED_SHARD):
    """Yield `(start_row, block)` in rowid order. Replayable — the streaming build reads twice."""
    with _get_conn(shard) as conn:
        _ensure_schema(conn)
        cur = conn.execute(
            "SELECT dtype, vector FROM vectors WHERE model = ? ORDER BY rowid", (model,)
        )
        start = 0
        while True:
            rows = cur.fetchmany(chunk)
            if not rows:
                break
            block = np.empty((len(rows), dim), dtype=np.float32)
            for i, r in enumerate(rows):
                block[i] = _decode(r[1], r[0], dim)
            yield start, block
            start += len(rows)


def _sample_vectors(model: str, dim: int, target: int, shard: str = SHARED_SHARD) -> np.ndarray:
    """
    A representative subset for fitting centroids.

    Uses SQLite's own RANDOM() ordering rather than reading everything and sampling in Python —
    the whole point is not to materialise the full set.
    """
    with _get_conn(shard) as conn:
        _ensure_schema(conn)
        rows = conn.execute(
            "SELECT dtype, vector FROM vectors WHERE model = ? ORDER BY RANDOM() LIMIT ?",
            (model, int(target)),
        ).fetchall()
    out = np.empty((len(rows), dim), dtype=np.float32)
    for i, r in enumerate(rows):
        out[i] = _decode(r[1], r[0], dim)
    return out


def _ann_search(query: np.ndarray, model: str, limit: int, shard: str = SHARED_SHARD) -> list[Hit] | None:
    """Approximate path. Returns None when there is no usable index, so the caller scans exactly."""
    directory = _ivf_dir(model, shard)
    paths_file = directory / "paths.json"
    if not paths_file.is_file():
        return None
    idx = vector_index.IvfIndex(directory)
    hits = idx.search(query, limit=limit * 2)   # over-fetch: some may have been deleted since
    if not hits:
        return None
    try:
        paths = json.loads(paths_file.read_text())
    except (OSError, ValueError):
        return None

    candidates = [(paths[h.row], h.score) for h in hits if 0 <= h.row < len(paths)]
    if not candidates:
        return []
    # Drop anything no longer in the store. The index is a snapshot; the table is the truth.
    names = [c[0] for c in candidates]
    placeholders = ",".join("?" * len(names))
    with _get_conn(shard) as conn:
        _ensure_schema(conn)
        live = {r[0] for r in conn.execute(
            f"SELECT rel_path FROM vectors WHERE model = ? AND rel_path IN ({placeholders})",
            (model, *names),
        ).fetchall()}
    return [Hit(rel_path=p, score=sc) for p, sc in candidates if p in live][:limit]


def search(query: Sequence[float] | np.ndarray, *, model: str, limit: int = 20,
           shard: str = SHARED_SHARD) -> list[Hit]:
    """
    Nearest stored vectors to [query], most similar first.

    An int8 index is searched in blocks rather than upcast whole: measured on the Cubie that is
    the difference between 178 ms and 67 ms at 100k items, and it also avoids briefly allocating
    a float32 copy four times the size of the index on the board least able to afford it.
    """
    q = _normalise(np.asarray(query, dtype=np.float32).reshape(-1))

    # Approximate first when a library is large enough to have one. Falls through to the exact
    # scan when no index exists, which is the state of every small library and of any large one
    # between an indexing run and its rebuild.
    approx = _ann_search(q, model, limit, shard)
    if approx is not None:
        return approx

    paths, index, dtype = _resident(model, shard)
    if index is None or not paths:
        return []
    if q.shape[0] != index.shape[1]:
        # Almost always a model change without a re-embed. Refusing beats returning a ranking
        # computed from two different vector spaces, which would look like results.
        raise ValueError(
            f"query has {q.shape[0]} dimensions but the index for model {model!r} "
            f"holds {index.shape[1]} — re-embed the library after changing model"
        )

    if dtype == "int8":
        # The 1/127 dequantisation scale rides on the *query* rather than the index. Dividing
        # each upcast block instead allocates a second full-size float32 temporary per block —
        # measured on the Cubie at 100k that alone was the difference between 484 ms and 69 ms.
        # Mathematically identical: (A/127) . q == A . (q/127).
        scaled_q = q / 127.0
        scores = np.empty(len(paths), dtype=np.float32)
        for start in range(0, len(paths), _SEARCH_BLOCK_ROWS):
            end = min(start + _SEARCH_BLOCK_ROWS, len(paths))
            scores[start:end] = index[start:end].astype(np.float32) @ scaled_q
    else:
        scores = index @ q

    k = min(limit, len(paths))
    top = np.argpartition(-scores, k - 1)[:k] if k < len(paths) else np.arange(len(paths))
    top = top[np.argsort(-scores[top])]
    return [Hit(rel_path=paths[i], score=float(scores[i])) for i in top]


# --- rescan support ---------------------------------------------------------

def rebuild_required(known: dict[str, str], *, model: str,
                     shard: str = SHARED_SHARD) -> tuple[list[str], list[str]]:
    """
    Compare the library against the store: what needs embedding, and what is now orphaned.

    [known] maps rel_path to its current content key. Returns (to_embed, to_delete).

    This is the whole of the store's "rebuild by rescan" contract — it reports, it does not
    repair. Losing this database costs a rescan and nothing else, so the honest interface is one
    that tells the caller what a rescan would have to do rather than pretending to self-heal.
    """
    with _get_conn(shard) as conn:
        _ensure_schema(conn)
        rows = conn.execute(
            "SELECT rel_path, content_key FROM vectors WHERE model = ?", (model,)
        ).fetchall()
    stored = {r["rel_path"]: r["content_key"] for r in rows}

    to_embed = [p for p, key in known.items() if stored.get(p) != key]
    to_delete = [p for p in stored if p not in known]
    return to_embed, to_delete


def rebuild_required_from_db(
    media_db: Path, model: str, *, limit: int | None = None, shard: str = SHARED_SHARD
) -> tuple[list[str], list[str]]:
    """
    The same comparison as [rebuild_required], done in SQL against `media.db` directly.

    [rebuild_required] takes the whole library as a `{path: key}` dict. At 10 lakh items that dict
    is roughly 300 MB of Python strings — more than the smallest board has free — so at production
    scale the caller must not build it. Both sides are SQLite, so the diff is an ATTACH and two
    joins, and costs a few MB regardless of library size.

    [limit] caps how many paths to embed in one pass, so a first run on a large library produces
    work in slices instead of one enormous list. Deletions are never capped: leaving a removed
    file searchable is worse than doing a little extra work.
    """
    with _get_conn(shard) as conn:
        _ensure_schema(conn)
        conn.execute("ATTACH DATABASE ? AS media", (str(media_db),))
        try:
            embed_sql = (
                "SELECT b.rel_path FROM media.blobs b "
                "LEFT JOIN vectors v ON v.rel_path = b.rel_path AND v.model = ? "
                "WHERE (v.rel_path IS NULL OR v.content_key <> b.content_hash) "
                "AND EXISTS (SELECT 1 FROM media.entries e "
                "           WHERE e.blob_id = b.id AND e.deleted = 0)"
            )
            params: tuple = (model,)
            if limit is not None:
                embed_sql += " LIMIT ?"
                params = (model, int(limit))
            to_embed = [r[0] for r in conn.execute(embed_sql, params).fetchall()]

            to_delete = [r[0] for r in conn.execute(
                "SELECT v.rel_path FROM vectors v "
                "LEFT JOIN media.blobs b ON b.rel_path = v.rel_path "
                "WHERE v.model = ? AND (b.rel_path IS NULL "
                "   OR NOT EXISTS (SELECT 1 FROM media.entries e "
                "                  WHERE e.blob_id = b.id AND e.deleted = 0))",
                (model,),
            ).fetchall()]
        finally:
            conn.execute("DETACH DATABASE media")
    return to_embed, to_delete


def content_keys_for(paths: Sequence[str], media_db: Path,
                     shard: str = SHARED_SHARD) -> dict[str, str]:
    """Content hashes for a batch of paths — enough to store alongside their vectors."""
    if not paths:
        return {}
    with _get_conn(shard) as conn:
        conn.execute("ATTACH DATABASE ? AS media", (str(media_db),))
        try:
            placeholders = ",".join("?" * len(paths))
            rows = conn.execute(
                f"SELECT rel_path, content_hash FROM media.blobs WHERE rel_path IN ({placeholders})",
                tuple(paths),
            ).fetchall()
        finally:
            conn.execute("DETACH DATABASE media")
    return {r[0]: r[1] for r in rows}


def upsert_many(
    items: Sequence[tuple[str, np.ndarray, str]],
    *,
    model: str,
    quantise: bool = False,
    expected_dim: int | None = None,
    shard: str = SHARED_SHARD,
) -> int:
    """
    Store a batch in one transaction.

    One commit per vector is what a naive loop does, and at a million items that is a million
    fsyncs — the dominant cost of the whole indexing run on an SD-backed board.
    """
    if not items:
        return 0
    rows = []
    for rel_path, vector, content_key in items:
        arr = np.asarray(vector, dtype=np.float32).reshape(-1)
        dim = int(arr.shape[0])
        if expected_dim is not None and dim != expected_dim:
            raise ValueError(f"expected {expected_dim} dimensions, got {dim} for {rel_path!r}")
        arr = _normalise(arr)
        if quantise:
            payload = np.clip(np.rint(arr * 127.0), -127, 127).astype(np.int8).tobytes()
            dtype = "int8"
        else:
            payload = arr.tobytes()
            dtype = "float32"
        rows.append((rel_path, dim, dtype, payload, model, content_key))

    with _get_conn(shard) as conn:
        _ensure_schema(conn)
        conn.executemany(
            "INSERT INTO vectors (rel_path, dim, dtype, vector, model, content_key) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(rel_path, model) DO UPDATE SET "
            "dim=excluded.dim, dtype=excluded.dtype, vector=excluded.vector, "
            "model=excluded.model, content_key=excluded.content_key",
            rows,
        )
        conn.commit()
    _invalidate(model, shard)
    return len(rows)


def stats(shard: str = SHARED_SHARD) -> dict:
    with _get_conn(shard) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n, SUM(LENGTH(vector)) AS bytes FROM vectors"
        ).fetchone()
        by_dtype = conn.execute(
            "SELECT dtype, COUNT(*) AS n FROM vectors GROUP BY dtype"
        ).fetchall()
    return {
        "count": row["n"] or 0,
        "vector_bytes": row["bytes"] or 0,
        "by_dtype": {r["dtype"]: r["n"] for r in by_dtype},
    }


def _reset_for_tests() -> None:
    _invalidate()
    close_pool()
    root = settings.data_dir / "vectors"
    if root.is_dir():
        for f in root.glob("*.db*"):
            with contextlib.suppress(OSError):
                f.unlink()
