"""
Background embedding of the library (Phase 4 milestones 1–2, the wiring).

Runs *behind* the metadata scan, never inside it. Metadata indexing must finish quickly — it is
what makes files appear in the app — while embedding 100k items costs 24 minutes on the fastest
board in this fleet and 2.8 hours on the slowest with the wrong model. Putting that on the scan
path would mean a NAS that stalls whenever someone uploads a photo.

## Single-flight, with coalescing

Only one embedding run exists at a time. A scan that starts while one is running does **not**
launch a second: it replaces the *pending* work and the running job picks it up on its next
batch. Two concurrent runs on a 937 MB board would compete for the same model in memory and
double the CPU on hardware that is also serving video.

## Resumability comes from the store, not from checkpoints

`job_store` persists only terminal jobs, so a job in flight does not survive a reboot — and it
does not need to. `vector_store.rebuild_required()` compares the library against what is already
embedded, so after any interruption the answer to "what is left" is simply that comparison run
again. Vectors already committed stay committed.

That is why this module holds no checkpoint file and no resume state: **the store is the
progress record.** A checkpoint would be a second source of truth that can disagree with it.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import embedding, embedding_context, job_store, vector_store, workload
from .config import settings

logger = logging.getLogger("aihomecloud.semantic_indexer")


@dataclass
class Progress:
    """What a client can show. Plain values so it serialises into `Job.progress` untouched."""

    total: int = 0
    processed: int = 0
    deleted: int = 0
    failed: int = 0
    model: str = ""
    started_at: float = field(default_factory=time.time)

    @property
    def remaining(self) -> int:
        return max(0, self.total - self.processed)

    def as_dict(self) -> dict:
        elapsed = max(1e-6, time.time() - self.started_at)
        rate = self.processed / elapsed
        # ETA only once there is a rate to extrapolate from. A number derived from two items is
        # worse than no number — it will be wildly wrong and shown to a user as a promise.
        eta = int(self.remaining / rate) if self.processed >= 20 and rate > 0 else None
        return {
            "phase": "embedding",
            "model": self.model,
            "total": self.total,
            "processed": self.processed,
            "remaining": self.remaining,
            "deleted": self.deleted,
            "failed": self.failed,
            "itemsPerSecond": round(rate, 2) if self.processed >= 20 else None,
            "etaSeconds": eta,
        }


def _known_shards(media_db) -> list[str]:
    """
    Every shard the library currently needs, derived from the paths in `media.db`.

    Derived rather than configured: a shard exists because a person's folder does, so reading the
    paths is the only source that cannot drift from reality.
    """
    import sqlite3
    shards = {vector_store.SHARED_SHARD}
    try:
        conn = sqlite3.connect(f"file:{media_db}?mode=ro", uri=True)
        try:
            for (rel_path,) in conn.execute("SELECT rel_path FROM blobs"):
                shards.add(vector_store.shard_for_path(rel_path))
        finally:
            conn.close()
    except Exception:
        logger.exception("could not enumerate shards; falling back to shared only")
        return [vector_store.SHARED_SHARD]
    return sorted(shards)


def _cpu_temperature() -> float | None:
    """
    This board's CPU temperature, or None where it cannot be read.

    Deliberately reads sysfs directly rather than going through `board.py`: this runs in a worker
    thread on a hot loop, and a missing or unreadable thermal zone must degrade to "unknown" — on
    a host without one, indexing should still run, just without thermal backoff.
    """
    for path in ("/sys/class/thermal/thermal_zone0/temp",
                 "/sys/devices/virtual/thermal/thermal_zone0/temp"):
        try:
            with open(path) as fh:
                raw = int(fh.read().strip())
            return raw / 1000.0 if raw > 1000 else float(raw)
        except (OSError, ValueError):
            continue
    return None


def thermal_pause_seconds(temp_c: float | None, base_throttle: float) -> float:
    """
    How long to rest after a batch, given the current temperature.

    Measured on the ROCK Pi 4A: a sustained index at the 50 ms default takes the SoC from 57 C to
    **85 C**, which is where an RK3399 begins thermal throttling — and a full 10 lakh library is
    close to seven hours at that temperature on a passively cooled board. A fixed duty cycle
    cannot know that; it is the same whether the board is cold or cooking.

    So the pause grows with temperature. Below 70 C the configured value stands. Above that it
    lengthens, and past 82 C it becomes long enough that the board sheds heat faster than the
    workload adds it. Indexing takes longer; the board does not sit at its throttle point for
    hours. That is the right trade for a background job nobody is waiting on.

    Returns [base_throttle] unchanged when the temperature is unknown — never a *shorter* pause
    than configured, since an unknown temperature is not evidence of a cool board.
    """
    if temp_c is None:
        return base_throttle
    if temp_c < 70.0:
        return base_throttle
    if temp_c < 76.0:
        return max(base_throttle, 0.15)
    if temp_c < 82.0:
        return max(base_throttle, 0.4)
    return max(base_throttle, 1.0)


class _State:
    """Module-level run state. One lock guards everything that decides whether a run exists."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.job_id: str | None = None
        self.pending: dict[str, str] | None = None
        self.media_db = None
        self.cancel = False
        self.progress: Progress | None = None


_state = _State()


def is_running() -> bool:
    with _state.lock:
        return _state.job_id is not None


def current_job_id() -> str | None:
    with _state.lock:
        return _state.job_id


def cancel() -> bool:
    """Ask the running job to stop between batches. Returns False if nothing is running."""
    with _state.lock:
        if _state.job_id is None:
            return False
        _state.cancel = True
        return True


def request_from_db(media_db, user_id: str = "") -> tuple[str | None, bool]:
    """
    Start (or extend) an indexing run that diffs against `media.db` in SQL.

    Preferred over [request] at any real scale: that one takes the whole library as a dict, which
    is ~300 MB of Python strings at 10 lakh items — more than the smallest board has free.
    """
    spec = embedding.spec_for()
    if not embedding.available(spec=spec):
        logger.info("semantic_index_skipped reason=unavailable model=%s", spec.name)
        return None, False

    with _state.lock:
        if _state.job_id is not None:
            _state.media_db = media_db
            logger.info("semantic_index_coalesced job=%s (db diff)", _state.job_id)
            return _state.job_id, False
        job = job_store.create_job(user_id=user_id)
        _state.job_id = job.id
        _state.media_db = media_db
        _state.pending = None
        _state.cancel = False
        _state.progress = Progress(model=spec.name)

    threading.Thread(target=_run_db, args=(job.id, spec, media_db),
                     daemon=True, name="semantic-index").start()
    logger.info("semantic_index_started job=%s mode=db model=%s", job.id, spec.name)
    return job.id, True


def _texts_for(batch, media_db) -> list[str]:
    """
    Path text plus whatever the other indexes know about these files.

    Enrichment is best-effort by design: if `media.db` or `docs.db` is missing or unreadable, this
    degrades to exactly the previous behaviour rather than failing the indexing run.
    """
    base = {p: embedding.humanise_path(p) for p in batch}
    try:
        extras = embedding_context.extras_for(
            list(batch), Path(media_db), settings.data_dir / "docs.db"
        )
    except Exception:  # never let enrichment break indexing
        logger.exception("semantic_index_enrich_failed size=%d", len(batch))
        extras = {}
    return [
        f"{base[p]} {extras[p]}".strip() if extras.get(p) else base[p]
        for p in batch
    ]


def _index_images(job_id: str, media_db, throttle: float) -> int:
    """
    Embed photos with the vision encoder, so they can be found by what is in them.

    Reuses the text path's diff, shard routing and thermal backoff unchanged — the store is keyed
    by `(rel_path, model)`, so image vectors sit beside text vectors in the same sharded databases
    and inherit the same privacy boundary for free.

    The vision encoder is released at the end. It is the expensive resident (77 MB against the
    text encoder's 27 MB) and is useless until the next indexing run.
    """
    from . import image_embedding

    if not image_embedding.available():
        return 0

    provider = image_embedding.ImageEmbeddingProvider()
    model = provider.model_name
    # Images cost ~486 ms each, so a large batch buys nothing and only delays the cancel check.
    batch_size = 8
    done = 0
    try:
        for shard in _known_shards(media_db):
            to_embed, to_delete = vector_store.rebuild_required_from_db(media_db, model, shard=shard)
            if to_delete:
                vector_store.delete(to_delete, shard=shard)
            # Only images. Handing an .mkv to the vision encoder wastes half a second per file and
            # produces a vector of its first frame at best.
            paths = [p for p in to_embed
                     if image_embedding.is_image(p) and vector_store.shard_for_path(p) == shard]
            if not paths:
                continue

            keys = vector_store.content_keys_for(paths, media_db, shard=shard)
            for start in range(0, len(paths), batch_size):
                with _state.lock:
                    if _state.cancel:
                        logger.info("image_index_cancelled job=%s", job_id)
                        return done
                batch = paths[start:start + batch_size]
                absolute = [settings.nas_root / p.lstrip("/") for p in batch]
                ok, vecs = provider.embed_images(absolute)
                if len(ok) == 0:
                    continue
                # embed_images skips unreadable files, so map results back by position rather than
                # assuming the batch survived intact — storing a good vector against the wrong path
                # would be silent and permanent.
                root = str(settings.nas_root)
                rows = []
                for abs_path, vec in zip(ok, vecs):
                    rel = "/" + str(abs_path)[len(root):].lstrip("/")
                    rows.append((rel, vec, keys.get(rel, "")))
                vector_store.upsert_many(rows, model=model, shard=shard)
                done += len(rows)

                # Thermal backoff protects the board; this protects the family. Embedding a
                # library is the definition of work nobody is waiting for.
                workload.gate_sync("semantic_index", max_wait=24 * 60 * 60)
                workload.gate_sync("image_index", max_wait=24 * 60 * 60)
                pause = thermal_pause_seconds(_cpu_temperature(), throttle)
                if pause:
                    time.sleep(pause)
        if done:
            logger.info("image_index_completed job=%s images=%d", job_id, done)
    finally:
        provider.release_vision()
    return done


def _run_db(job_id: str, spec: embedding.ModelSpec, media_db) -> None:
    """
    Indexing at production scale: the work list comes from SQL a slice at a time, vectors are
    written in batched transactions, and the loop yields the CPU between batches.

    Resumability is unchanged and still needs no checkpoint — every completed batch is committed,
    and the SQL diff on the next pass returns exactly what is still missing.
    """
    try:
        provider = embedding.OnnxEmbeddingProvider(spec=spec)
        batch_size = max(1, int(getattr(settings, "embedding_batch_size", 32)))
        slice_size = max(batch_size, int(getattr(settings, "embedding_slice_size", 20_000)))
        throttle = max(0, int(getattr(settings, "embedding_throttle_ms", 50))) / 1000.0
        quantise = bool(getattr(settings, "embedding_quantise", False))
        total_done = 0

        while True:
            with _state.lock:
                if _state.cancel:
                    cancelled = True
                else:
                    cancelled = False
                    media_db = _state.media_db or media_db
            if cancelled:
                logger.info("semantic_index_cancelled job=%s", job_id)
                _finish(job_id, job_store.JobStatus.completed)
                return

            # Per shard. A user's own library and the shared one are separate databases, so the
            # diff, the writes and any index build all happen against the right file.
            shards = _known_shards(media_db)
            to_embed_by_shard: dict[str, list[str]] = {}
            to_delete_total = 0
            for shard in shards:
                emb, dele = vector_store.rebuild_required_from_db(
                    media_db, spec.name, limit=slice_size, shard=shard
                )
                emb = [p for p in emb if vector_store.shard_for_path(p) == shard]
                if emb:
                    to_embed_by_shard[shard] = emb
                if dele:
                    vector_store.delete(dele, shard=shard)
                    to_delete_total += len(dele)
            to_embed = [p for paths in to_embed_by_shard.values() for p in paths]
            to_delete = []
            if to_delete_total:
                with _state.lock:
                    if _state.progress:
                        _state.progress.deleted += to_delete_total
            if not to_embed:
                break

            with _state.lock:
                if _state.progress:
                    _state.progress.total = total_done + len(to_embed)

            keys = vector_store.content_keys_for(to_embed, media_db)
            for start in range(0, len(to_embed), batch_size):
                with _state.lock:
                    cancelled = _state.cancel
                if cancelled:
                    logger.info("semantic_index_cancelled job=%s", job_id)
                    _finish(job_id, job_store.JobStatus.completed)
                    return

                batch = to_embed[start:start + batch_size]
                try:
                    vectors = provider.embed_documents(_texts_for(batch, media_db))
                    # Grouped by shard so each write lands in the right database.
                    by_shard: dict[str, list] = {}
                    for path, vec in zip(batch, vectors):
                        by_shard.setdefault(vector_store.shard_for_path(path), []).append(
                            (path, vec, keys.get(path, ""))
                        )
                    for shard, rows in by_shard.items():
                        vector_store.upsert_many(
                            rows, model=spec.name, quantise=quantise,
                            expected_dim=spec.dim, shard=shard,
                        )
                except Exception:
                    logger.exception("semantic_index_batch_failed job=%s size=%d", job_id, len(batch))
                    with _state.lock:
                        if _state.progress:
                            _state.progress.failed += len(batch)
                    continue

                total_done += len(batch)
                snapshot = None
                with _state.lock:
                    if _state.progress:
                        _state.progress.processed += len(batch)
                        snapshot = _state.progress.as_dict()
                if snapshot is not None:
                    job_store.update_job(job_id, progress=snapshot)

                # Rest between batches, longer when the board is hot. A fixed duty cycle is the
                # same whether the SoC is at 57 C or 85 C; this is not.
                # Thermal backoff protects the board; this protects the family. Embedding a
                # library is the definition of work nobody is waiting for.
                workload.gate_sync("semantic_index", max_wait=24 * 60 * 60)
                pause = thermal_pause_seconds(_cpu_temperature(), throttle)
                if pause:
                    time.sleep(pause)

        # Per shard: only one that has actually grown past the threshold gets an index, which
        # in practice means the shared family shard rather than anyone's personal library.
        for shard in _known_shards(media_db):
            try:
                ann = vector_store.build_ann_index(spec.name, shard=shard)
                if ann.get("built"):
                    logger.info("semantic_index_ann_built job=%s shard=%s %s", job_id, shard, ann)
            except Exception:
                logger.exception("semantic_index_ann_build_failed job=%s shard=%s", job_id, shard)

        # Images second, and only if this board has the model. Kept a separate pass rather than
        # woven into the loop above: it uses a different model, a different space and a different
        # cost per item (486 ms against ~2 ms), so interleaving would make both harder to reason
        # about and neither faster.
        try:
            _index_images(job_id, media_db, throttle)
        except Exception:
            logger.exception("image_index_failed job=%s", job_id)

        _finish(job_id, job_store.JobStatus.completed)
        logger.info("semantic_index_completed job=%s items=%d", job_id, total_done)

    except Exception as exc:  # noqa: BLE001 — a worker thread must never die silently
        logger.exception("semantic_index_failed job=%s", job_id)
        _finish(job_id, job_store.JobStatus.failed, error=str(exc))


def request(known: dict[str, str], user_id: str = "") -> tuple[str | None, bool]:
    """
    Ask for [known] — `{rel_path: content_key}` for the whole library — to be embedded.

    Returns `(job_id, started)`. When a run is already in flight this replaces its pending work
    and returns `(existing_job_id, False)` rather than starting a second one. Callers should
    treat that as success: the work is queued, not refused.

    Returns `(None, False)` when this board cannot embed at all, so a caller can skip silently
    rather than create a job that immediately fails.
    """
    spec = embedding.spec_for()
    if not embedding.available(spec=spec):
        logger.info("semantic_index_skipped reason=unavailable model=%s", spec.name)
        return None, False

    with _state.lock:
        if _state.job_id is not None:
            _state.pending = dict(known)
            logger.info("semantic_index_coalesced job=%s items=%d", _state.job_id, len(known))
            return _state.job_id, False

        job = job_store.create_job(user_id=user_id)
        _state.job_id = job.id
        _state.pending = dict(known)
        _state.cancel = False
        _state.progress = Progress(model=spec.name)

    threading.Thread(target=_run, args=(job.id, spec), daemon=True, name="semantic-index").start()
    logger.info("semantic_index_started job=%s items=%d model=%s", job.id, len(known), spec.name)
    return job.id, True


def _take_pending() -> dict[str, str] | None:
    with _state.lock:
        pending, _state.pending = _state.pending, None
        return pending


def _finish(job_id: str, status: job_store.JobStatus, error: str | None = None) -> None:
    with _state.lock:
        progress = _state.progress.as_dict() if _state.progress else None
        _state.job_id = None
        _state.pending = None
        _state.media_db = None
        _state.cancel = False
    job_store.update_job(job_id, status=status, error=error, progress=progress)


def _run(job_id: str, spec: embedding.ModelSpec) -> None:
    """
    The worker. Deliberately synchronous and on its own thread: inference is CPU-bound, and
    running it on the event loop would stall every request on these single-digit-core boards.
    """
    try:
        provider = embedding.OnnxEmbeddingProvider(spec=spec)
        batch_size = max(1, int(getattr(settings, "embedding_batch_size", 32)))
        quantise = bool(getattr(settings, "embedding_quantise", False))

        while True:
            known = _take_pending()
            if known is None:
                break

            to_embed, to_delete = vector_store.rebuild_required(known, model=spec.name)

            with _state.lock:
                progress = _state.progress or Progress(model=spec.name)
                # `total` counts this pass. A coalesced scan resets it, which is honest — the
                # remaining work genuinely changed — rather than showing a bar that goes backwards
                # against a stale denominator.
                progress.total = len(to_embed)
                progress.processed = 0
                _state.progress = progress

            if to_delete:
                vector_store.delete(to_delete)
                with _state.lock:
                    if _state.progress:
                        _state.progress.deleted += len(to_delete)

            for start in range(0, len(to_embed), batch_size):
                with _state.lock:
                    cancelled = _state.cancel
                # _finish takes the same lock, and threading.Lock is not reentrant — calling it
                # from inside the block above deadlocks the worker permanently on cancel.
                if cancelled:
                    logger.info("semantic_index_cancelled job=%s", job_id)
                    _finish(job_id, job_store.JobStatus.completed)
                    return

                batch = to_embed[start:start + batch_size]
                texts = _texts_for(batch, settings.data_dir / "media.db")
                try:
                    vectors = provider.embed_documents(texts)
                except Exception:
                    # One bad batch must not abandon the library. Skip it, count it, continue —
                    # the next scan will retry those paths because they still have no vector.
                    logger.exception("semantic_index_batch_failed job=%s size=%d", job_id, len(batch))
                    with _state.lock:
                        if _state.progress:
                            _state.progress.failed += len(batch)
                    continue

                for rel_path, vector in zip(batch, vectors):
                    vector_store.upsert(
                        rel_path, vector,
                        model=spec.name, content_key=known[rel_path],
                        quantise=quantise, expected_dim=spec.dim,
                    )

                snapshot = None
                with _state.lock:
                    if _state.progress:
                        _state.progress.processed += len(batch)
                        snapshot = _state.progress.as_dict()
                if snapshot is not None:
                    job_store.update_job(job_id, progress=snapshot)

                # Yield the CPU between batches. Without this the run pins its threads for the
                # whole job, which is what turns a long index into a thermal problem on a
                # passively cooled board that is also serving video.
                time.sleep(0)

        # Build the approximate index once the embedding pass is done, not per batch: clustering
        # is a whole-collection operation and doing it repeatedly during a multi-hour run would
        # cost more than the embedding. A no-op below the size threshold.
        try:
            ann = vector_store.build_ann_index(spec.name)
            if ann.get("built"):
                logger.info("semantic_index_ann_built job=%s %s", job_id, ann)
        except Exception:
            # An index that fails to build leaves exact search working, just slower. That is a
            # degraded feature, not a failed one, so the job still completes.
            logger.exception("semantic_index_ann_build_failed job=%s", job_id)

        _finish(job_id, job_store.JobStatus.completed)
        logger.info("semantic_index_completed job=%s", job_id)

    except Exception as exc:  # noqa: BLE001 — a worker thread must never die silently
        logger.exception("semantic_index_failed job=%s", job_id)
        _finish(job_id, job_store.JobStatus.failed, error=str(exc))


async def request_async(known: dict[str, str], user_id: str = "") -> tuple[str | None, bool]:
    """Async-friendly wrapper; `request` only starts a thread, so it does not block meaningfully."""
    return await asyncio.get_running_loop().run_in_executor(None, request, known, user_id)


def _reset_for_tests() -> None:
    with _state.lock:
        _state.job_id = None
        _state.pending = None
        _state.media_db = None
        _state.cancel = False
        _state.progress = None
