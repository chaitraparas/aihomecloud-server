"""
The background embedding job.

Concurrency, not embedding, is what this suite is about. The failures that matter — two runs
competing for one model on a 937 MB board, a cancel that deadlocks the worker, a job that
reports progress against a stale denominator — are all invisible to a functional test that just
checks vectors appear.
"""

import threading
import time

import numpy as np
import pytest

from app import embedding, job_store, semantic_indexer as si, vector_store as vs

MODEL = "bge-small-en-v1.5"


class FakeProvider:
    """Counts calls and can be made slow, so a second request lands mid-run."""

    def __init__(self, spec, delay: float = 0.0):
        self.spec = spec
        self.delay = delay
        self.batches: list[int] = []
        self.lock = threading.Lock()

    @property
    def model_name(self):
        return self.spec.name

    def embed_documents(self, texts):
        if self.delay:
            time.sleep(self.delay)
        with self.lock:
            self.batches.append(len(texts))
        rng = np.random.default_rng(len(texts))
        v = rng.standard_normal((len(texts), self.spec.dim)).astype(np.float32)
        return v / np.linalg.norm(v, axis=1, keepdims=True)

    def embed_query(self, text):
        return np.ones(self.spec.dim, dtype=np.float32) / np.sqrt(self.spec.dim)


@pytest.fixture(autouse=True)
def clean(tmp_path, monkeypatch):
    monkeypatch.setattr(vs.settings, "data_dir", tmp_path)
    monkeypatch.setattr(embedding, "available", lambda *a, **k: True)
    # Throttling is for multi-hour production runs; it would only make tests slow.
    monkeypatch.setattr(si.settings, "embedding_throttle_ms", 0)
    vs._reset_for_tests(); vs.init_db()
    si._reset_for_tests()
    yield
    si._reset_for_tests()
    vs.close_pool()


def install_provider(monkeypatch, delay: float = 0.0) -> FakeProvider:
    holder = {}

    def factory(*args, **kwargs):
        spec = kwargs.get("spec") or embedding.spec_for()
        p = holder.get("p")
        if p is None:
            p = FakeProvider(spec, delay)
            holder["p"] = p
        return p

    monkeypatch.setattr(embedding, "OnnxEmbeddingProvider", factory)
    return holder


def wait_until(pred, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


class TestUnavailable:
    def test_returns_no_job_when_the_board_cannot_embed(self, monkeypatch):
        # Creating a job that immediately fails would fill the job list with noise on every
        # board that simply does not have the runtime — which is most of them.
        monkeypatch.setattr(embedding, "available", lambda *a, **k: False)

        job_id, started = si.request({"a.jpg": "k"})

        assert job_id is None and started is False
        assert si.is_running() is False


class TestSingleFlight:
    def test_a_second_request_coalesces_instead_of_starting_another_run(self, monkeypatch):
        holder = install_provider(monkeypatch, delay=0.15)

        first, started_first = si.request({f"a{i}.jpg": "k" for i in range(64)})
        assert started_first is True
        assert wait_until(lambda: si.is_running())

        second, started_second = si.request({f"b{i}.jpg": "k" for i in range(8)})

        # Same job, not a new one — two runs would compete for one model in memory.
        assert second == first
        assert started_second is False
        assert wait_until(lambda: not si.is_running(), timeout=15)

    def test_the_coalesced_work_is_actually_embedded(self, monkeypatch):
        install_provider(monkeypatch, delay=0.1)

        si.request({f"a{i}.jpg": "k" for i in range(32)})
        assert wait_until(lambda: si.is_running())
        si.request({"late.jpg": "k"})

        assert wait_until(lambda: not si.is_running(), timeout=15)
        # The late arrival must not be dropped just because a run was already going.
        stored = {h.rel_path for h in vs.search(np.ones(384, dtype=np.float32), model=MODEL, limit=100)}
        assert "late.jpg" in stored


class TestCancel:
    def test_cancel_stops_the_run_without_deadlocking(self, monkeypatch):
        install_provider(monkeypatch, delay=0.1)
        si.request({f"a{i}.jpg": "k" for i in range(200)})
        assert wait_until(lambda: si.is_running())

        assert si.cancel() is True

        # The bug this guards: _finish takes the same non-reentrant lock as the cancel check, so
        # calling it from inside that block hung the worker forever.
        assert wait_until(lambda: not si.is_running(), timeout=10), "worker did not stop — deadlock?"

    def test_cancel_with_nothing_running_is_false(self):
        assert si.cancel() is False


class TestProgress:
    def test_progress_reports_counts_and_withholds_eta_until_meaningful(self, monkeypatch):
        install_provider(monkeypatch)
        job_id, _ = si.request({f"a{i}.jpg": "k" for i in range(10)})
        assert wait_until(lambda: not si.is_running(), timeout=15)

        job = job_store.get_job(job_id)
        p = job.progress

        assert p["processed"] == 10
        assert p["remaining"] == 0
        assert p["model"] == MODEL
        # Under 20 items there is no rate worth extrapolating; a made-up ETA shown to a user is
        # worse than none.
        assert p["etaSeconds"] is None

    def test_the_job_ends_completed(self, monkeypatch):
        install_provider(monkeypatch)
        job_id, _ = si.request({"a.jpg": "k"})
        assert wait_until(lambda: not si.is_running(), timeout=15)

        assert job_store.get_job(job_id).status == job_store.JobStatus.completed


class TestRescanSemantics:
    def test_unchanged_files_are_not_re_embedded(self, monkeypatch):
        holder = install_provider(monkeypatch)
        known = {f"a{i}.jpg": "k" for i in range(8)}

        si.request(known)
        assert wait_until(lambda: not si.is_running(), timeout=15)
        first_batches = list(holder["p"].batches)

        si.request(known)          # same content keys — nothing to do
        assert wait_until(lambda: not si.is_running(), timeout=15)

        assert holder["p"].batches == first_batches, "re-embedded files that had not changed"

    def test_a_removed_file_is_deleted_from_the_store(self, monkeypatch):
        install_provider(monkeypatch)
        si.request({"keep.jpg": "k", "gone.jpg": "k"})
        assert wait_until(lambda: not si.is_running(), timeout=15)

        si.request({"keep.jpg": "k"})
        assert wait_until(lambda: not si.is_running(), timeout=15)

        stored = {h.rel_path for h in vs.search(np.ones(384, dtype=np.float32), model=MODEL, limit=50)}
        assert stored == {"keep.jpg"}


class TestResilience:
    def test_a_failing_batch_does_not_abandon_the_library(self, monkeypatch):
        spec = embedding.spec_for()

        class Flaky(FakeProvider):
            def embed_documents(self, texts):
                if any("bad" in t for t in texts):
                    raise RuntimeError("inference blew up")
                return super().embed_documents(texts)

        monkeypatch.setattr(embedding, "OnnxEmbeddingProvider", lambda *a, **k: Flaky(spec))
        monkeypatch.setattr(si.settings, "embedding_batch_size", 1)

        job_id, _ = si.request({"good1.jpg": "k", "bad.jpg": "k", "good2.jpg": "k"})
        assert wait_until(lambda: not si.is_running(), timeout=15)

        job = job_store.get_job(job_id)
        assert job.status == job_store.JobStatus.completed
        assert job.progress["failed"] == 1
        stored = {h.rel_path for h in vs.search(np.ones(384, dtype=np.float32), model=MODEL, limit=50)}
        assert stored == {"good1.jpg", "good2.jpg"}


class TestProductionScalePath:
    """
    The SQL-diff path, which is what runs at real scale. The dict path stays for small callers,
    but at 10 lakh items that dict is ~300 MB of Python strings on a 937 MB board.
    """

    @staticmethod
    def _media_db(tmp_path, entries):
        import sqlite3
        p = tmp_path / "media.db"
        c = sqlite3.connect(p)
        # Mirrors the real media_index schema: `blobs` is content, `entries` carries `deleted`.
        # The fixture used to omit `entries` entirely, which meant these tests could not have
        # caught the diff indexing deleted files.
        c.execute("CREATE TABLE blobs (id INTEGER PRIMARY KEY, rel_path TEXT UNIQUE, "
                  "content_hash TEXT)")
        c.execute("CREATE TABLE entries (id INTEGER PRIMARY KEY, blob_id INTEGER, "
                  "deleted INTEGER NOT NULL DEFAULT 0)")
        for i, (rel, h) in enumerate(entries.items(), start=1):
            c.execute("INSERT INTO blobs VALUES (?,?,?)", (i, rel, h))
            c.execute("INSERT INTO entries (blob_id, deleted) VALUES (?, 0)", (i,))
        c.commit(); c.close()
        return p

    def test_it_embeds_everything_media_db_knows_about(self, tmp_path, monkeypatch):
        install_provider(monkeypatch)
        db = self._media_db(tmp_path, {f"/srv/nas/a{i}.jpg": f"h{i}" for i in range(20)})

        job_id, started = si.request_from_db(db)
        assert started
        assert wait_until(lambda: not si.is_running(), timeout=20)

        assert job_store.get_job(job_id).status == job_store.JobStatus.completed
        assert vs.index_count(MODEL) == 20

    def test_a_changed_hash_re_embeds_only_that_file(self, tmp_path, monkeypatch):
        holder = install_provider(monkeypatch)
        entries = {f"/srv/nas/a{i}.jpg": f"h{i}" for i in range(10)}
        db = self._media_db(tmp_path, entries)
        si.request_from_db(db); assert wait_until(lambda: not si.is_running(), timeout=20)
        first = sum(holder["p"].batches)

        # Same library, one file's content changed.
        import sqlite3
        c = sqlite3.connect(db)
        c.execute("UPDATE blobs SET content_hash='changed' WHERE rel_path=?", ("/srv/nas/a3.jpg",))
        c.commit(); c.close()

        si.request_from_db(db); assert wait_until(lambda: not si.is_running(), timeout=20)

        assert sum(holder["p"].batches) == first + 1, "re-embedded more than the changed file"

    def test_a_file_removed_from_media_db_is_dropped(self, tmp_path, monkeypatch):
        install_provider(monkeypatch)
        db = self._media_db(tmp_path, {"/srv/nas/keep.jpg": "h1", "/srv/nas/gone.jpg": "h2"})
        si.request_from_db(db); assert wait_until(lambda: not si.is_running(), timeout=20)

        import sqlite3
        c = sqlite3.connect(db); c.execute("DELETE FROM blobs WHERE rel_path=?", ("/srv/nas/gone.jpg",))
        c.commit(); c.close()

        si.request_from_db(db); assert wait_until(lambda: not si.is_running(), timeout=20)

        assert vs.index_count(MODEL) == 1

    def test_nothing_to_do_completes_immediately(self, tmp_path, monkeypatch):
        install_provider(monkeypatch)
        db = self._media_db(tmp_path, {"/srv/nas/a.jpg": "h"})
        si.request_from_db(db); assert wait_until(lambda: not si.is_running(), timeout=20)

        job_id, _ = si.request_from_db(db)
        assert wait_until(lambda: not si.is_running(), timeout=20)

        assert job_store.get_job(job_id).status == job_store.JobStatus.completed


class TestThermalBackoff:
    """
    Measured on the ROCK Pi: a sustained index at the 50 ms default takes the SoC from 57 C to
    85 C, the RK3399's throttle point, and a full library is ~7 hours of that. A fixed duty cycle
    cannot respond to it.
    """

    def test_a_cool_board_uses_the_configured_pause(self):
        assert si.thermal_pause_seconds(55.0, 0.05) == 0.05

    def test_the_pause_grows_with_temperature(self):
        warm = si.thermal_pause_seconds(72.0, 0.05)
        hot = si.thermal_pause_seconds(79.0, 0.05)
        very_hot = si.thermal_pause_seconds(88.0, 0.05)

        assert 0.05 < warm < hot < very_hot

    def test_an_unknown_temperature_never_shortens_the_pause(self):
        # A board with no readable thermal zone is not evidence of a cool board.
        assert si.thermal_pause_seconds(None, 0.2) == 0.2

    def test_a_longer_configured_pause_is_never_overridden_downwards(self):
        # An operator who asked for a slow index gets one, hot or cold.
        assert si.thermal_pause_seconds(60.0, 2.0) == 2.0
        assert si.thermal_pause_seconds(90.0, 2.0) == 2.0

    def test_reading_the_temperature_never_raises(self):
        # It runs in a hot loop on hosts that may not have a thermal zone at all.
        assert si._cpu_temperature() is None or isinstance(si._cpu_temperature(), float)
