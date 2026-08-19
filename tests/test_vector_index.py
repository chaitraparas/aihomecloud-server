"""
The IVF index.

IVF is *approximate*, so the tests that matter are the ones that quantify what is given up.
A structural test ("it returns 20 results") passes just as happily on an index that returns
20 wrong results, and that failure is invisible in production — search just feels bad.
"""

import numpy as np
import pytest

from app.vector_index import IvfIndex, choose_cluster_count, choose_probe_count


def clustered(n: int, dim: int = 32, groups: int = 12, seed: int = 0) -> np.ndarray:
    """Vectors with real structure — random noise has no clusters, so IVF cannot help on it."""
    rng = np.random.default_rng(seed)
    centres = rng.standard_normal((groups, dim)).astype(np.float32)
    centres /= np.linalg.norm(centres, axis=1, keepdims=True)
    which = rng.integers(0, groups, size=n)
    v = centres[which] + rng.standard_normal((n, dim)).astype(np.float32) * 0.25
    return (v / np.linalg.norm(v, axis=1, keepdims=True)).astype(np.float32)


def exact_top(vectors: np.ndarray, q: np.ndarray, k: int) -> list[int]:
    s = vectors @ q
    return np.argsort(-s)[:k].tolist()


class TestSizing:
    def test_cluster_count_tracks_sqrt_n(self):
        assert choose_cluster_count(1_000_000) == pytest.approx(1000, rel=0.05)
        assert choose_cluster_count(10_000) == 100

    def test_cluster_count_is_clamped_for_tiny_and_huge(self):
        # A tiny library must not get one vector per cluster; a huge one must not get so many
        # centroids that comparing against them becomes the cost.
        assert choose_cluster_count(10) == 64
        assert choose_cluster_count(10**9) == 4096

    def test_probe_count_is_a_small_fraction_but_never_trivial(self):
        assert choose_probe_count(1000) == 50
        assert choose_probe_count(64) == 8


class TestRecall:
    """What IVF costs. These numbers are the reason the exact path still exists."""

    @pytest.mark.parametrize("n", [5_000, 20_000])
    def test_recall_at_10_is_at_least_90_percent(self, tmp_path, n):
        vectors = clustered(n)
        idx = IvfIndex(tmp_path)
        idx.build(vectors, seed=1)

        rng = np.random.default_rng(7)
        hits = 0
        trials = 25
        for _ in range(trials):
            q = vectors[rng.integers(0, n)]
            want = set(exact_top(vectors, q, 10))
            got = {h.row for h in idx.search(q, limit=10)}
            hits += len(want & got) / 10
        recall = hits / trials
        assert recall >= 0.90, f"recall@10 was {recall:.2f}"

    def test_the_exact_nearest_neighbour_is_almost_always_first(self, tmp_path):
        # A query that *is* a stored vector should find itself. If clustering put it in a cluster
        # the query does not probe, it will not — which is precisely the approximation.
        vectors = clustered(8_000)
        idx = IvfIndex(tmp_path)
        idx.build(vectors, seed=2)

        rng = np.random.default_rng(3)
        found = 0
        for _ in range(40):
            i = int(rng.integers(0, len(vectors)))
            hits = idx.search(vectors[i], limit=1)
            found += bool(hits and hits[0].row == i)
        assert found / 40 >= 0.95

    def test_more_probes_never_reduces_recall(self, tmp_path):
        vectors = clustered(6_000)
        idx = IvfIndex(tmp_path)
        idx.build(vectors, seed=4)
        q = vectors[100]
        want = set(exact_top(vectors, q, 10))

        few = {h.row for h in idx.search(q, limit=10, n_probe=2)}
        many = {h.row for h in idx.search(q, limit=10, n_probe=64)}

        assert len(many & want) >= len(few & want)


class TestOrdering:
    def test_results_come_back_best_first(self, tmp_path):
        vectors = clustered(3_000)
        idx = IvfIndex(tmp_path)
        idx.build(vectors, seed=5)

        hits = idx.search(vectors[42], limit=10)
        scores = [h.score for h in hits]

        assert scores == sorted(scores, reverse=True)

    def test_rows_map_back_to_the_callers_order(self, tmp_path):
        # The index stores vectors grouped by cluster, which is *not* the caller's order. If the
        # row mapping were wrong every result would point at the wrong file — and still look
        # like a plausible ranking.
        vectors = clustered(2_000)
        idx = IvfIndex(tmp_path)
        idx.build(vectors, seed=6)

        i = 777
        hits = idx.search(vectors[i], limit=1)
        assert hits[0].row == i
        assert np.dot(vectors[hits[0].row], vectors[i]) == pytest.approx(1.0, abs=1e-4)


class TestLifecycle:
    def test_search_without_an_index_returns_nothing_rather_than_raising(self, tmp_path):
        # Callers fall back to an exact scan on empty, so this must not be an error.
        assert IvfIndex(tmp_path).search(np.ones(32, dtype=np.float32)) == []

    def test_building_from_zero_vectors_is_refused(self, tmp_path):
        with pytest.raises(ValueError, match="zero vectors"):
            IvfIndex(tmp_path).build(np.empty((0, 32), dtype=np.float32))

    def test_a_wrong_width_query_is_refused(self, tmp_path):
        idx = IvfIndex(tmp_path)
        idx.build(clustered(500), seed=8)
        with pytest.raises(ValueError, match="dims"):
            idx.search(np.ones(999, dtype=np.float32))

    def test_drop_removes_every_file(self, tmp_path):
        idx = IvfIndex(tmp_path)
        idx.build(clustered(500), seed=9)
        assert idx.exists()

        idx.drop()

        assert not idx.exists()
        assert list(tmp_path.glob("ivf_*")) == []

    def test_an_index_survives_a_fresh_process(self, tmp_path):
        # It is on disk; a restart must not require a rebuild of something that took hours.
        vectors = clustered(1_500)
        IvfIndex(tmp_path).build(vectors, seed=10)

        reopened = IvfIndex(tmp_path)
        hits = reopened.search(vectors[11], limit=3)

        assert hits and hits[0].row == 11

    def test_an_unrecognised_index_format_is_ignored_not_fatal(self, tmp_path):
        idx = IvfIndex(tmp_path)
        idx.build(clustered(500), seed=11)
        idx._meta_path.write_text('{"magic": "something-else"}')

        # A future format change must degrade to "no index" — the caller then scans exactly —
        # rather than crashing search for the whole library.
        assert IvfIndex(tmp_path).search(np.ones(32, dtype=np.float32)) == []


class TestStats:
    def test_stats_expose_cluster_health(self, tmp_path):
        idx = IvfIndex(tmp_path)
        idx.build(clustered(4_000), seed=12)

        s = idx.stats()

        assert s["present"] is True
        assert s["count"] == 4_000
        # Empty clusters are expected and harmless; a wildly dominant cluster is not, because it
        # would make one probe cost as much as a full scan.
        assert s["largest_cluster"] < 4_000 * 0.5


class TestStreamingBuild:
    """
    The streaming build exists because the in-memory one needs 1.5 GB at a million vectors, which
    the smallest board cannot hold. It must produce an index indistinguishable from the other.
    """

    def test_it_matches_the_in_memory_build(self, tmp_path):
        vectors = clustered(3_000)

        a = IvfIndex(tmp_path / "mem")
        a.build(vectors, seed=1)

        def chunks():
            for s in range(0, len(vectors), 500):
                yield s, vectors[s:s + 500]

        b = IvfIndex(tmp_path / "stream")
        b.build_streaming(count=len(vectors), dim=vectors.shape[1],
                          chunks=chunks, sample=vectors, seed=1)

        q = vectors[123]
        # Same seed and same sample, so the centroids and therefore the results should agree.
        assert [h.row for h in a.search(q, limit=5)] == [h.row for h in b.search(q, limit=5)]

    def test_rows_still_map_to_the_callers_order(self, tmp_path):
        # The streaming writer computes positions from a cursor rather than a sort; an off-by-one
        # there would point every result at the wrong file while still ranking plausibly.
        vectors = clustered(2_000)

        def chunks():
            for s in range(0, len(vectors), 333):    # deliberately not a divisor
                yield s, vectors[s:s + 333]

        idx = IvfIndex(tmp_path)
        idx.build_streaming(count=len(vectors), dim=vectors.shape[1],
                            chunks=chunks, sample=vectors, seed=2)

        for probe_row in (0, 1, 999, 1999):
            hits = idx.search(vectors[probe_row], limit=1)
            assert hits[0].row == probe_row

    def test_zero_count_is_refused(self, tmp_path):
        with pytest.raises(ValueError, match="zero vectors"):
            IvfIndex(tmp_path).build_streaming(
                count=0, dim=32, chunks=lambda: iter(()), sample=clustered(10)
            )
