"""
Sub-linear vector search for libraries too large to scan (Phase 4, production scale).

`vector_store`'s exact scan holds every vector in RAM and compares all of them per query. Measured
on the ROCK Pi, that dies around 250k items:

    N          float32              int8 (blocked)
    100k       154 MB /  39 ms      38 MB /  58 ms
    250k       384 MB /  66 ms      96 MB / 152 ms
    500k       768 MB / 137 ms     192 MB / 293 ms
    1M        1536 MB / 294 ms     384 MB / 588 ms

At the target of 10 lakh items neither dtype fits the budget, and 1.5 GB resident does not fit a
937 MB board at all. int8 is no rescue — it is consistently *slower*, because the upcast dominates
the dot product. **The problem is the linear scan, not the element type.**

## What this does instead

An IVF index: the vectors are clustered, stored on disk grouped by cluster, and a query compares
itself against the cluster centroids first, then scans only the few nearest clusters.

At 1M items with 1024 clusters, a query touches roughly 20k vectors instead of 1,000,000 — and the
table above says 20k costs single-digit milliseconds. Resident memory is the centroids (1.5 MB)
plus whatever the OS keeps paged in from the clusters actually read.

The vectors live in a memory-mapped file, so **RAM usage is bounded by what is being read, not by
the library size.** That is the property that makes a 937 MB board viable at a million items.

## Why not hnswlib or faiss

Both are faster still, and both keep the full vector set resident — which is the constraint that
actually binds here. They also add a compiled dependency to boards where `onnxruntime` is already
the heaviest thing installed. numpy plus mmap does what is needed.

## What is given up, honestly

IVF is **approximate**. A vector whose true nearest neighbour sits in an unprobed cluster is
missed. Recall rises with `n_probe` and is measured in `test_vector_index.py` rather than assumed —
the exact-search path remains available and is what small libraries use.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np

logger = logging.getLogger("aihomecloud.vector_index")

# Below this, an exact scan is faster than the machinery here and is exact.
#
# Lowered from 200k to 100k after measuring the PRODUCTION board rather than the dev one. On the
# Cubie A5E (937 MB, 8 cores): 40k scans in 11 ms, 100k in 27 ms, 200k in **65 ms** — and the
# search budget is only ~52 ms once the 23 ms query embedding is paid. 200k was over budget on the
# machine that actually matters, while looking fine on the 3.8 GB dev board.
#
# Per shard, not globally. With per-user sharding a user's own library is 10-40k, so this
# effectively arms only for the shared family shard, which is the one that grows.
IVF_MIN_ITEMS = 100_000

_MAGIC = "ahc-ivf-1"


@dataclass(frozen=True)
class IvfHit:
    row: int
    score: float


def choose_cluster_count(n: int) -> int:
    """
    Roughly sqrt(n), clamped.

    sqrt(n) balances the two halves of the search: comparing against centroids costs O(k), and
    scanning the probed clusters costs O(n_probe * n/k). Both grow badly if k is far off, and the
    minimum stops tiny libraries getting one vector per cluster.
    """
    return int(max(64, min(4096, round(np.sqrt(max(1, n))))))


def choose_probe_count(k: int) -> int:
    """
    How many clusters to scan. ~5% of them, at least 8.

    Recall against this is measured, not assumed — see the recall tests. Fewer probes is faster
    and misses more; this is the point on that curve that held ≥95% recall in testing.
    """
    return int(max(8, min(k, round(k * 0.05))))


class IvfIndex:
    """
    A clustered, memory-mapped vector index.

    Built once from the full vector set and rebuilt when it drifts — there is no incremental
    re-clustering, deliberately. Assigning a new vector to its nearest existing centroid is cheap
    and correct enough; only when the *distribution* moves do the centroids need redoing, and a
    rebuild at these sizes costs a couple of minutes against a library that took hours to embed.
    """

    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self._lock = threading.Lock()
        self._centroids: np.ndarray | None = None
        self._offsets: np.ndarray | None = None   # cluster i occupies rows [offsets[i], offsets[i+1])
        self._rows: np.ndarray | None = None      # original row id for each stored position
        self._mmap: np.memmap | None = None
        self._dim = 0
        self._count = 0

    # --- paths ---
    @property
    def _meta_path(self) -> Path: return self.dir / "ivf_meta.json"
    @property
    def _vec_path(self) -> Path: return self.dir / "ivf_vectors.f32"
    @property
    def _cent_path(self) -> Path: return self.dir / "ivf_centroids.f32"
    @property
    def _rows_path(self) -> Path: return self.dir / "ivf_rows.i64"

    def exists(self) -> bool:
        return self._meta_path.is_file() and self._vec_path.is_file()

    # --- build ---

    def build(self, vectors: np.ndarray, *, seed: int = 0, iterations: int = 10) -> dict:
        """
        Cluster [vectors] and write the index to disk.

        [vectors] must be unit-normalised, one row per item, in the caller's row order — that
        order is what [search] returns, so the caller can map results back without storing paths
        here.
        """
        if vectors.ndim != 2:
            raise ValueError("vectors must be 2-D")
        n, dim = vectors.shape
        if n == 0:
            raise ValueError("cannot build an index from zero vectors")

        k = choose_cluster_count(n)
        centroids = _kmeans(vectors, k, seed=seed, iterations=iterations)
        assign = _assign(vectors, centroids)

        # Group rows by cluster so each cluster is one contiguous slab on disk. Contiguity is the
        # whole point: probing a cluster becomes a single sequential read rather than scattered
        # page faults across the file.
        counts = np.bincount(assign, minlength=len(centroids))
        offsets = np.zeros(len(centroids) + 1, dtype=np.int64)
        np.cumsum(counts, out=offsets[1:])

        self.dir.mkdir(parents=True, exist_ok=True)

        # Written through a memmap, one source chunk at a time. The obvious `vectors[order]`
        # materialises a second full copy — 1.5 GB at a million items — and OOM-killed this on
        # the ROCK Pi, which has 3.4 GB free. The build must not need twice the index in RAM.
        cursor = offsets[:-1].copy()
        out = np.memmap(self._vec_path, dtype=np.float32, mode="w+", shape=(n, dim))
        rows_out = np.empty(n, dtype=np.int64)
        chunk = 20_000
        for start in range(0, n, chunk):
            end = min(start + chunk, n)
            block = vectors[start:end]
            block_assign = assign[start:end]
            # Sorting only within the chunk keeps writes mostly sequential per cluster without
            # ever holding a full reordering.
            local_order = np.argsort(block_assign, kind="stable")
            for local in local_order:
                c = int(block_assign[local])
                pos = int(cursor[c]); cursor[c] += 1
                out[pos] = block[local]
                rows_out[pos] = start + int(local)
        out.flush()
        del out

        centroids.astype(np.float32).tofile(self._cent_path)
        rows_out.tofile(self._rows_path)
        self._meta_path.write_text(json.dumps({
            "magic": _MAGIC, "count": int(n), "dim": int(dim),
            "clusters": int(len(centroids)), "offsets": offsets.tolist(),
        }))

        self._reset()
        logger.info("ivf_built items=%d dim=%d clusters=%d", n, dim, len(centroids))
        return {"count": int(n), "dim": int(dim), "clusters": int(len(centroids))}

    def build_streaming(
        self,
        *,
        count: int,
        dim: int,
        chunks,
        sample: np.ndarray,
        seed: int = 0,
        iterations: int = 10,
    ) -> dict:
        """
        Build without ever holding the whole vector set in RAM.

        [chunks] is a callable returning a fresh iterator of `(start_row, block)` — it is consumed
        **twice**, once to assign and once to write, so it must be replayable.
        [sample] is a representative subset used to fit the centroids; the caller draws it, since
        only the caller knows how to sample its own storage cheaply.

        This exists because `build()` requires the full matrix resident: 1.5 GB at a million
        vectors, which the smallest board in this fleet (937 MB) cannot hold. Search was never the
        problem there — the build was.
        """
        if count <= 0:
            raise ValueError("cannot build an index from zero vectors")

        k = choose_cluster_count(count)
        centroids = _kmeans(sample, k, seed=seed, iterations=iterations)

        # Pass 1: assign every vector, holding only one chunk plus the assignment array
        # (4 bytes per vector — 4 MB at a million).
        assign = np.empty(count, dtype=np.int32)
        for start, block in chunks():
            assign[start:start + block.shape[0]] = _assign(block, centroids)

        counts = np.bincount(assign, minlength=len(centroids))
        offsets = np.zeros(len(centroids) + 1, dtype=np.int64)
        np.cumsum(counts, out=offsets[1:])

        # Pass 2: write each vector to its cluster's slot in the memmap.
        self.dir.mkdir(parents=True, exist_ok=True)
        cursor = offsets[:-1].copy()
        out = np.memmap(self._vec_path, dtype=np.float32, mode="w+", shape=(count, dim))
        rows_out = np.empty(count, dtype=np.int64)
        for start, block in chunks():
            block_assign = assign[start:start + block.shape[0]]
            for local in np.argsort(block_assign, kind="stable"):
                c = int(block_assign[local])
                pos = int(cursor[c]); cursor[c] += 1
                out[pos] = block[local]
                rows_out[pos] = start + int(local)
        out.flush()
        del out

        centroids.astype(np.float32).tofile(self._cent_path)
        rows_out.tofile(self._rows_path)
        self._meta_path.write_text(json.dumps({
            "magic": _MAGIC, "count": int(count), "dim": int(dim),
            "clusters": int(len(centroids)), "offsets": offsets.tolist(),
        }))
        self._reset()
        logger.info("ivf_built_streaming items=%d dim=%d clusters=%d", count, dim, len(centroids))
        return {"count": int(count), "dim": int(dim), "clusters": int(len(centroids))}

    # --- load / search ---

    def _reset(self) -> None:
        with self._lock:
            self._centroids = None
            self._offsets = None
            self._rows = None
            self._mmap = None

    def _ensure_loaded(self) -> bool:
        if self._mmap is not None:
            return True
        with self._lock:
            if self._mmap is not None:
                return True
            if not self.exists():
                return False
            meta = json.loads(self._meta_path.read_text())
            if meta.get("magic") != _MAGIC:
                logger.warning("ivf_meta_unrecognised magic=%r — ignoring index", meta.get("magic"))
                return False
            self._count = int(meta["count"])
            self._dim = int(meta["dim"])
            self._offsets = np.asarray(meta["offsets"], dtype=np.int64)
            self._centroids = np.fromfile(self._cent_path, dtype=np.float32).reshape(-1, self._dim)
            self._rows = np.fromfile(self._rows_path, dtype=np.int64)
            # mmap, not fromfile: the file may be 1.5 GB and the point is that RAM tracks what is
            # read, not what exists.
            self._mmap = np.memmap(self._vec_path, dtype=np.float32, mode="r",
                                   shape=(self._count, self._dim))
            logger.info("ivf_loaded items=%d clusters=%d", self._count, len(self._centroids))
            return True

    def search(self, query: np.ndarray, *, limit: int = 20, n_probe: int | None = None) -> list[IvfHit]:
        """
        Approximate nearest rows to [query], best first, as original row ids.

        Returns [] when no index is present, so a caller can fall back to an exact scan rather
        than treating a missing index as an error.
        """
        if not self._ensure_loaded():
            return []
        q = np.asarray(query, dtype=np.float32).reshape(-1)
        if q.shape[0] != self._dim:
            raise ValueError(f"query has {q.shape[0]} dims, index holds {self._dim}")

        centroids, offsets, rows, data = self._centroids, self._offsets, self._rows, self._mmap
        probes = n_probe or choose_probe_count(len(centroids))
        probes = max(1, min(probes, len(centroids)))

        # Nearest centroids first — this is the step that makes the search sub-linear.
        cent_scores = centroids @ q
        nearest = np.argpartition(-cent_scores, probes - 1)[:probes]

        best_scores: list[np.ndarray] = []
        best_rows: list[np.ndarray] = []
        for c in nearest:
            start, end = int(offsets[c]), int(offsets[c + 1])
            if end <= start:
                continue
            block = np.asarray(data[start:end])   # only this slab is paged in
            scores = block @ q
            best_scores.append(scores)
            best_rows.append(rows[start:end])

        if not best_scores:
            return []
        scores = np.concatenate(best_scores)
        row_ids = np.concatenate(best_rows)
        k = min(limit, scores.shape[0])
        top = np.argpartition(-scores, k - 1)[:k] if k < scores.shape[0] else np.arange(scores.shape[0])
        top = top[np.argsort(-scores[top])]
        return [IvfHit(row=int(row_ids[i]), score=float(scores[i])) for i in top]

    def stats(self) -> dict:
        if not self._ensure_loaded():
            return {"present": False}
        sizes = np.diff(self._offsets)
        return {
            "present": True,
            "count": self._count,
            "dim": self._dim,
            "clusters": int(len(self._centroids)),
            "n_probe": choose_probe_count(len(self._centroids)),
            "largest_cluster": int(sizes.max()),
            "empty_clusters": int((sizes == 0).sum()),
            "bytes_on_disk": int(self._vec_path.stat().st_size),
        }

    def drop(self) -> None:
        self._reset()
        for p in (self._meta_path, self._vec_path, self._cent_path, self._rows_path):
            p.unlink(missing_ok=True)


# --- clustering --------------------------------------------------------------

def _kmeans(vectors: np.ndarray, k: int, *, seed: int, iterations: int) -> np.ndarray:
    """
    Spherical k-means on a sample, with k-means++ seeding.

    Clustering a million vectors to convergence would take longer than embedding them, so the
    centroids are fitted on a sample and every vector is then assigned exactly.

    **The sample must be large relative to k, and the seeding must not be random.** The first
    version used `max(k*20, 20_000)` points with random initialisation, which at k=2000 gave
    twenty samples per centroid — and measured recall@10 of 21–26% at 500k, with recall tracking
    the fraction of vectors scanned almost linearly. That is the signature of centroids that do
    not describe the data: probing more clusters helps only because it scans more, not because it
    scans the right ones. Both were the cause; neither was the IVF approach.
    """
    rng = np.random.default_rng(seed)
    n = vectors.shape[0]
    # ~100 points per centroid. Below roughly 40 the centroids are fitted to noise.
    sample_size = min(n, max(k * 100, 50_000))
    sample = vectors[rng.choice(n, size=sample_size, replace=False)] if sample_size < n else vectors
    k = min(k, sample.shape[0])

    centroids = _kmeanspp(sample, k, rng)
    for _ in range(iterations):
        assign = _assign(sample, centroids)
        # Vectorised update: summing members per cluster with one scatter-add beats a Python loop
        # over k clusters, which at k=4000 dominated the build.
        sums = np.zeros_like(centroids)
        np.add.at(sums, assign, sample)
        counts = np.bincount(assign, minlength=k)[:, None]
        moved = counts[:, 0] > 0
        norms = np.linalg.norm(sums[moved], axis=1, keepdims=True)
        centroids[moved] = np.divide(sums[moved], np.clip(norms, 1e-9, None))
        # An empty cluster keeps its previous centroid rather than being reseeded — reseeding
        # mid-loop makes the result depend on the iteration count in ways that are hard to reason
        # about, and an empty cluster is never probed so it costs nothing.
    return centroids.astype(np.float32)


def _kmeanspp(sample: np.ndarray, k: int, rng: np.random.Generator) -> np.ndarray:
    """
    k-means++ seeding: each new centroid is drawn with probability proportional to its squared
    distance from the nearest chosen one.

    Random seeding puts several centroids inside the same dense region and leaves other regions
    unrepresented, which is exactly what produced 22% recall. This costs k passes over the sample
    and pays for itself many times over.
    """
    n = sample.shape[0]
    centroids = np.empty((k, sample.shape[1]), dtype=np.float32)
    centroids[0] = sample[rng.integers(0, n)]
    # Cosine on unit vectors: distance^2 = 2 - 2*similarity, so tracking max similarity is enough.
    best_sim = sample @ centroids[0]
    for i in range(1, k):
        d2 = np.clip(2.0 - 2.0 * best_sim, 0.0, None)
        total = float(d2.sum())
        if total <= 1e-12:
            centroids[i] = sample[rng.integers(0, n)]
        else:
            centroids[i] = sample[int(rng.choice(n, p=d2 / total))]
        np.maximum(best_sim, sample @ centroids[i], out=best_sim)
    return centroids


def _assign(vectors: np.ndarray, centroids: np.ndarray, block: int = 20_000) -> np.ndarray:
    """Nearest centroid per vector, in blocks so the score matrix never materialises whole."""
    out = np.empty(vectors.shape[0], dtype=np.int32)
    for start in range(0, vectors.shape[0], block):
        end = min(start + block, vectors.shape[0])
        out[start:end] = np.argmax(vectors[start:end] @ centroids.T, axis=1)
    return out
