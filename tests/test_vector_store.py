"""
Vector storage for semantic search.

The failure this suite is really guarding against is silence: a store that returns *some* ranking
whatever you put in it. A wrong dimension, a mixed-up quantisation scale or a stale rebuild
comparison all produce plausible-looking results rather than errors, and would be discovered as
"search feels bad" months later.
"""

import numpy as np
import pytest

from app import vector_store as vs

MODEL = "bge-small-en-v1.5"


@pytest.fixture(autouse=True)
def fresh_store(tmp_path, monkeypatch):
    monkeypatch.setattr(vs.settings, "data_dir", tmp_path)
    vs._reset_for_tests()
    vs.init_db()
    yield
    vs.close_pool()


def unit(seed: int) -> np.ndarray:
    v = np.random.default_rng(seed).standard_normal(vs.DIM).astype(np.float32)
    return v / np.linalg.norm(v)


class TestRoundTrip:
    def test_the_stored_vector_is_the_one_returned_first(self):
        target = unit(1)
        vs.upsert("photos/a.jpg", target, model=MODEL, content_key="k1")
        vs.upsert("photos/b.jpg", unit(2), model=MODEL, content_key="k2")

        hits = vs.search(target, model=MODEL, limit=2)

        assert hits[0].rel_path == "photos/a.jpg"
        assert hits[0].score == pytest.approx(1.0, abs=1e-5)

    def test_upsert_replaces_rather_than_duplicates(self):
        vs.upsert("photos/a.jpg", unit(1), model=MODEL, content_key="k1")
        vs.upsert("photos/a.jpg", unit(2), model=MODEL, content_key="k2")

        assert vs.stats()["count"] == 1
        # and the *second* vector is the one that survived
        assert vs.search(unit(2), model=MODEL, limit=1)[0].score == pytest.approx(1.0, abs=1e-5)

    def test_results_are_ordered_by_similarity(self):
        q = unit(1)
        vs.upsert("near.jpg", q, model=MODEL, content_key="a")
        vs.upsert("mid.jpg", (q + unit(2) * 0.5), model=MODEL, content_key="b")
        vs.upsert("far.jpg", -q, model=MODEL, content_key="c")

        order = [h.rel_path for h in vs.search(q, model=MODEL, limit=3)]

        assert order == ["near.jpg", "mid.jpg", "far.jpg"]


class TestDimensionIsEnforced:
    def test_a_vector_that_disagrees_with_its_model_is_refused(self):
        # Accepting this would not raise later — it would silently produce meaningless scores.
        with pytest.raises(ValueError, match="expected 384 dimensions"):
            vs.upsert("bad.jpg", np.zeros(128, dtype=np.float32),
                      model=MODEL, content_key="k", expected_dim=384)

    def test_a_query_of_the_wrong_width_is_refused(self):
        vs.upsert("a.jpg", unit(1), model=MODEL, content_key="k")
        # The realistic cause is switching models without re-embedding. Returning a ranking
        # computed across two vector spaces would look like results.
        with pytest.raises(ValueError, match="re-embed"):
            vs.search(np.zeros(128, dtype=np.float32), model=MODEL)

    def test_an_empty_vector_is_refused(self):
        with pytest.raises(ValueError, match="empty vector"):
            vs.upsert("bad.jpg", np.zeros(0, dtype=np.float32), model=MODEL, content_key="k")


class TestPerModelWidth:
    """bge-small is 384, base 768, large 1024 — the store must not assume one of them."""

    def test_a_768_wide_model_round_trips(self):
        v = np.random.default_rng(5).standard_normal(768).astype(np.float32)
        v /= np.linalg.norm(v)

        vs.upsert("a.jpg", v, model="bge-base-en-v1.5", content_key="k", expected_dim=768)
        hits = vs.search(v, model="bge-base-en-v1.5", limit=1)

        assert hits[0].rel_path == "a.jpg"
        assert hits[0].score == pytest.approx(1.0, abs=1e-5)

    def test_two_models_of_different_widths_coexist(self):
        small = unit(1)
        big = np.random.default_rng(6).standard_normal(768).astype(np.float32)
        big /= np.linalg.norm(big)
        vs.upsert("small.jpg", small, model=MODEL, content_key="k", expected_dim=384)
        vs.upsert("big.jpg", big, model="bge-base-en-v1.5", content_key="k", expected_dim=768)

        # Each search sees only its own model's vectors, at its own width.
        assert [h.rel_path for h in vs.search(small, model=MODEL, limit=5)] == ["small.jpg"]
        assert [h.rel_path for h in vs.search(big, model="bge-base-en-v1.5", limit=5)] == ["big.jpg"]

    def test_mixed_widths_under_one_model_are_refused_loudly(self):
        # Cannot happen through this module, but if it ever did, every score would be
        # meaningless — so it must not be quietly reshaped into something plausible.
        vs.upsert("a.jpg", unit(1), model=MODEL, content_key="k")
        wide = np.random.default_rng(7).standard_normal(768).astype(np.float32)
        vs.upsert("b.jpg", wide, model=MODEL, content_key="k")  # no expected_dim, so accepted

        with pytest.raises(ValueError, match="mixed widths"):
            vs.search(unit(1), model=MODEL)


class TestQuantisation:
    def test_int8_preserves_ranking(self):
        # int8 loses precision; what must survive is the *order*, since that is all search uses.
        q = unit(7)
        for name, v, key in (("near", q, "a"), ("mid", q + unit(8) * 0.6, "b"), ("far", -q, "c")):
            vs.upsert(f"{name}.jpg", v, model=MODEL, content_key=key, quantise=True)

        order = [h.rel_path for h in vs.search(q, model=MODEL, limit=3)]

        assert order == ["near.jpg", "mid.jpg", "far.jpg"]
        assert vs.stats()["by_dtype"] == {"int8": 3}

    def test_int8_scores_stay_close_to_float32(self):
        q = unit(3)
        vs.upsert("f32.jpg", q, model=MODEL, content_key="a")
        f32_score = vs.search(q, model=MODEL, limit=1)[0].score

        vs._reset_for_tests(); vs.init_db()
        vs.upsert("i8.jpg", q, model=MODEL, content_key="a", quantise=True)
        i8_score = vs.search(q, model=MODEL, limit=1)[0].score

        # 127-step symmetric quantisation of a unit vector; a scale error would show up here as
        # a score far from 1.0 rather than as a slightly noisier one.
        assert i8_score == pytest.approx(f32_score, abs=0.01)

    def test_a_part_migrated_store_is_still_searchable(self):
        # Quantisation is per-row so a library can be migrated in batches. That is only useful if
        # a half-migrated store still ranks correctly across both representations.
        q = unit(11)
        vs.upsert("f32.jpg", q, model=MODEL, content_key="a")
        vs.upsert("i8.jpg", -q, model=MODEL, content_key="b", quantise=True)

        hits = vs.search(q, model=MODEL, limit=2)

        assert [h.rel_path for h in hits] == ["f32.jpg", "i8.jpg"]


class TestModelIsolation:
    def test_vectors_from_another_model_are_not_searched(self):
        # Scores across different embedding models are meaningless together; mixing them would
        # return confident nonsense rather than nothing.
        vs.upsert("a.jpg", unit(1), model="other-model", content_key="k")

        assert vs.search(unit(1), model=MODEL) == []


class TestRebuildByRescan:
    def test_reports_new_changed_and_orphaned(self):
        vs.upsert("keep.jpg", unit(1), model=MODEL, content_key="same")
        vs.upsert("changed.jpg", unit(2), model=MODEL, content_key="old")
        vs.upsert("gone.jpg", unit(3), model=MODEL, content_key="whatever")

        to_embed, to_delete = vs.rebuild_required(
            {"keep.jpg": "same", "changed.jpg": "new", "brand_new.jpg": "fresh"}, model=MODEL
        )

        assert sorted(to_embed) == ["brand_new.jpg", "changed.jpg"]
        assert to_delete == ["gone.jpg"]

    def test_an_empty_store_asks_for_everything(self):
        to_embed, to_delete = vs.rebuild_required({"a.jpg": "k1", "b.jpg": "k2"}, model=MODEL)

        assert sorted(to_embed) == ["a.jpg", "b.jpg"]
        assert to_delete == []


class TestEdges:
    def test_search_on_an_empty_store_returns_nothing(self):
        assert vs.search(unit(1), model=MODEL) == []

    def test_limit_larger_than_the_store_is_not_an_error(self):
        vs.upsert("a.jpg", unit(1), model=MODEL, content_key="k")
        assert len(vs.search(unit(1), model=MODEL, limit=50)) == 1

    def test_a_zero_vector_does_not_produce_nan(self):
        # Normalising zero would give NaN, and a single NaN poisons every comparison after it.
        vs.upsert("zero.jpg", np.zeros(vs.DIM, dtype=np.float32), model=MODEL, content_key="k")
        vs.upsert("real.jpg", unit(1), model=MODEL, content_key="k2")

        hits = vs.search(unit(1), model=MODEL, limit=2)

        assert all(not np.isnan(h.score) for h in hits)
        assert hits[0].rel_path == "real.jpg"

    def test_delete_removes_only_what_was_asked(self):
        vs.upsert("a.jpg", unit(1), model=MODEL, content_key="k")
        vs.upsert("b.jpg", unit(2), model=MODEL, content_key="k")

        vs.delete(["a.jpg"])

        assert [h.rel_path for h in vs.search(unit(2), model=MODEL, limit=5)] == ["b.jpg"]


class TestSchemaCreatesItself:
    """
    A shard's schema is created when its connection is first opened.

    This replaces a guard that raised "init_db() was not called" — which was correct when schema
    creation was a separate startup step that could be forgotten, and once was, shipping a feature
    that failed instantly with a bare `no such table`. Sharding removes the possibility: a shard
    appears when a family member does, so there is no boot-time moment when the full set is known.
    """

    def test_a_brand_new_shard_is_usable_immediately(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vs.settings, "data_dir", tmp_path)
        vs._reset_for_tests()          # deliberately no init_db()

        vs.upsert("a.jpg", unit(1), model=MODEL, content_key="k", shard="brand_new")

        assert vs.index_count(MODEL, shard="brand_new") == 1

    def test_searching_an_untouched_shard_is_empty_not_an_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vs.settings, "data_dir", tmp_path)
        vs._reset_for_tests()

        assert vs.search(unit(1), model=MODEL, shard="never_used") == []


class TestApproximatePath:
    """
    The exact and approximate paths must be interchangeable from the caller's side, and the
    approximate one must never resurrect a deleted file — the index is a snapshot, the table is
    the truth.
    """

    def test_below_the_threshold_no_index_is_built(self):
        for i in range(10):
            vs.upsert(f"a{i}.jpg", unit(i), model=MODEL, content_key="k")

        result = vs.build_ann_index(MODEL)

        # An exact scan of ten vectors beats approximating them, and is exact.
        assert result["built"] is False
        assert "threshold" in result["reason"]

    def test_search_still_works_with_no_ann_index(self):
        target = unit(1)
        vs.upsert("a.jpg", target, model=MODEL, content_key="k")
        vs.upsert("b.jpg", unit(2), model=MODEL, content_key="k")

        hits = vs.search(target, model=MODEL, limit=2)

        assert hits[0].rel_path == "a.jpg"

    def test_a_deleted_file_cannot_come_back_from_a_stale_index(self, tmp_path, monkeypatch):
        # Build a small index by hand, bypassing the threshold, then delete a row from the store.
        # The index still contains it; the search must not.
        import json
        from app import vector_index

        for i in range(60):
            vs.upsert(f"f{i}.jpg", unit(i), model=MODEL, content_key="k")
        paths, matrix, _ = vs._load_all(MODEL)
        d = vs._ivf_dir(MODEL)
        vector_index.IvfIndex(d).build(matrix, seed=0)
        (d / "paths.json").write_text(json.dumps(paths))

        gone = paths[0]
        vs.delete([gone])

        hits = vs.search(unit(0), model=MODEL, limit=20)

        assert all(h.rel_path != gone for h in hits), "a deleted file was returned from the index"

    def test_index_count_reports_per_model(self):
        vs.upsert("a.jpg", unit(1), model=MODEL, content_key="k")
        vs.upsert("b.jpg", unit(2), model="other", content_key="k")

        assert vs.index_count(MODEL) == 1
        assert vs.index_count("other") == 1


class TestSharding:
    """
    One database per user plus one shared, because measured on the production board a 40k shard
    scans in 11 ms where a single 200k index takes 65 ms — and 15 concurrent searches over one
    100k index take 194 ms median, since BLAS is already multi-threaded and parallel searches
    contend. A family NAS has several people searching at once by definition.
    """

    def test_shards_do_not_see_each_other(self):
        mine = unit(1)
        vs.upsert("mine.jpg", mine, model=MODEL, content_key="k", shard="paras")
        vs.upsert("theirs.jpg", unit(2), model=MODEL, content_key="k", shard="prutha")

        assert [h.rel_path for h in vs.search(mine, model=MODEL, shard="paras")] == ["mine.jpg"]
        assert [h.rel_path for h in vs.search(mine, model=MODEL, shard="prutha")] == ["theirs.jpg"]

    def test_each_shard_is_its_own_file(self, tmp_path):
        vs.upsert("a.jpg", unit(1), model=MODEL, content_key="k", shard="paras")
        vs.upsert("b.jpg", unit(2), model=MODEL, content_key="k", shard=vs.SHARED_SHARD)

        names = {p.name for p in (vs.settings.data_dir / "vectors").glob("*.db")}
        assert "paras.db" in names and f"{vs.SHARED_SHARD}.db" in names

    def test_deleting_from_one_shard_leaves_the_others(self):
        vs.upsert("x.jpg", unit(1), model=MODEL, content_key="k", shard="paras")
        vs.upsert("x.jpg", unit(1), model=MODEL, content_key="k", shard="prutha")

        vs.delete(["x.jpg"], shard="paras")

        assert vs.index_count(MODEL, shard="paras") == 0
        assert vs.index_count(MODEL, shard="prutha") == 1

    def test_counts_are_per_shard(self):
        for i in range(3):
            vs.upsert(f"a{i}.jpg", unit(i), model=MODEL, content_key="k", shard="paras")
        vs.upsert("s.jpg", unit(9), model=MODEL, content_key="k", shard=vs.SHARED_SHARD)

        assert vs.index_count(MODEL, shard="paras") == 3
        assert vs.index_count(MODEL, shard=vs.SHARED_SHARD) == 1


class TestShardKeySafety:
    """A shard name becomes a filename, so it must not be able to choose where the file lands."""

    @pytest.mark.parametrize("raw", ["../../etc/passwd", "paras", "with space", "semi;colon", "a/b"])
    def test_no_path_syntax_survives(self, raw):
        key = vs.shard_key(raw)
        # Dots are excluded entirely: allowing them let "../../etc/passwd" sanitise to
        # ".._.._etc_passwd", which still carries ".." while looking safe.
        assert "/" not in key and "\\" not in key and "." not in key

    def test_empty_or_missing_falls_back_to_shared(self):
        # A caller that has not resolved an identity should still search something sensible.
        assert vs.shard_key(None) == vs.SHARED_SHARD
        assert vs.shard_key("") == vs.SHARED_SHARD
        assert vs.shard_key("   ") == vs.SHARED_SHARD

    def test_a_long_name_is_truncated(self):
        assert len(vs.shard_key("x" * 500)) <= 64


class TestResidentShardCap:
    def test_only_a_few_shards_stay_resident(self):
        # An unbounded cache of resident matrices is how a 937 MB board dies.
        for i in range(6):
            vs.upsert("a.jpg", unit(i), model=MODEL, content_key="k", shard=f"user{i}")
        for i in range(6):
            vs.search(unit(i), model=MODEL, shard=f"user{i}")

        assert len(vs._cached) <= vs._MAX_RESIDENT_SHARDS


class TestShardRouting:
    """
    Which shard a file lands in, and which shards a search covers. Getting this wrong is not a
    performance bug — it decides whether one family member's search can see another's library.
    """

    @pytest.fixture(autouse=True)
    def nas_layout(self, monkeypatch):
        monkeypatch.setattr(vs.settings, "nas_root", "/srv/nas")
        monkeypatch.setattr(vs.settings, "personal_base", "personal")

    @pytest.mark.parametrize("path,expected", [
        ("/srv/nas/personal/paras/Photos/a.jpg", "paras"),
        ("/srv/nas/personal/prutha/Docs/b.pdf", "prutha"),
        ("/srv/nas/family/Videos/c.mp4", vs.SHARED_SHARD),
        ("/srv/nas/entertainment/Movies/d.mkv", vs.SHARED_SHARD),
        ("/srv/nas/personal", vs.SHARED_SHARD),          # no user segment
        ("family/Photos/e.jpg", vs.SHARED_SHARD),        # already relative
    ])
    def test_files_route_to_the_right_shard(self, path, expected):
        assert vs.shard_for_path(path) == expected

    def test_a_search_covers_own_plus_shared(self):
        assert vs.shards_for_user("paras") == ["paras", vs.SHARED_SHARD]

    def test_an_anonymous_search_sees_only_shared(self):
        # Never another member's personal library by default.
        assert vs.shards_for_user(None) == [vs.SHARED_SHARD]

    def test_merged_search_ranks_across_shards(self):
        target = unit(1)
        vs.upsert("mine.jpg", target, model=MODEL, content_key="k", shard="paras")
        vs.upsert("shared.jpg", target * 0.5 + unit(2) * 0.5, model=MODEL,
                  content_key="k", shard=vs.SHARED_SHARD)

        hits = vs.search_shards(target, model=MODEL, shards=["paras", vs.SHARED_SHARD], limit=5)

        assert [h.rel_path for h in hits] == ["mine.jpg", "shared.jpg"]
        assert hits[0].score > hits[1].score

    def test_merged_search_does_not_reach_other_users(self):
        vs.upsert("theirs.jpg", unit(1), model=MODEL, content_key="k", shard="prutha")

        hits = vs.search_shards(unit(1), model=MODEL,
                                shards=vs.shards_for_user("paras"), limit=5)

        assert hits == []

    def test_duplicate_shards_are_searched_once(self):
        vs.upsert("a.jpg", unit(1), model=MODEL, content_key="k", shard=vs.SHARED_SHARD)

        hits = vs.search_shards(unit(1), model=MODEL,
                                shards=[vs.SHARED_SHARD, vs.SHARED_SHARD], limit=5)

        assert len(hits) == 1


class TestLegacyMigration:
    """
    Upgrading to the sharded layout used to leave the old `vectors.db` unread — search returned
    zero results with no error, which is indistinguishable from "nothing matched". These tests
    reproduce that failure shape, not just the happy path.
    """

    def _write_legacy(self, tmp_path, rows):
        import sqlite3

        legacy = tmp_path / "vectors.db"
        conn = sqlite3.connect(legacy)
        conn.execute(
            "CREATE TABLE vectors (rel_path TEXT PRIMARY KEY, dim INTEGER NOT NULL, "
            "dtype TEXT NOT NULL, vector BLOB NOT NULL, model TEXT NOT NULL, "
            "content_key TEXT NOT NULL)"
        )
        for rel_path, vec in rows:
            v = (vec / np.linalg.norm(vec)).astype(np.float32)
            conn.execute(
                "INSERT INTO vectors VALUES (?, ?, ?, ?, ?, ?)",
                (rel_path, vs.DIM, "float32", v.tobytes(), MODEL, "k"),
            )
        conn.commit()
        conn.close()
        return legacy

    def test_a_legacy_index_is_searchable_again_after_migration(self, tmp_path):
        target = unit(11)
        self._write_legacy(tmp_path, [("family/Photos/a.jpg", target)])

        # The failure being guarded against: readable file on disk, empty search.
        assert vs.search(target, model=MODEL, limit=5) == []

        vs.migrate_legacy_db()

        hits = vs.search_shards(
            target, model=MODEL, shards=vs.shards_for_user("anyone"), limit=5
        )
        assert [h.rel_path for h in hits] == ["family/Photos/a.jpg"]

    def test_rows_land_in_the_same_shard_a_fresh_index_would_use(self, tmp_path):
        personal = f"{vs.settings.nas_root}/personal/Paras/tax.pdf".replace("//", "/")
        self._write_legacy(
            tmp_path, [(personal, unit(12)), ("family/Photos/shared.jpg", unit(13))]
        )

        result = vs.migrate_legacy_db()

        assert result["migrated"] == 2
        assert result["by_shard"][vs.shard_for_path(personal)] == 1
        assert result["by_shard"][vs.SHARED_SHARD] == 1

    def test_the_legacy_file_is_retired_not_deleted(self, tmp_path):
        legacy = self._write_legacy(tmp_path, [("family/a.jpg", unit(14))])

        vs.migrate_legacy_db()

        assert not legacy.exists()
        assert (tmp_path / "vectors.db.pre-shard").exists(), "rollback copy must survive"

    def test_migrating_twice_is_a_no_op(self, tmp_path):
        self._write_legacy(tmp_path, [("family/a.jpg", unit(15))])

        first = vs.migrate_legacy_db()
        second = vs.migrate_legacy_db()

        assert first["migrated"] == 1
        assert second["migrated"] == 0

    def test_no_legacy_file_is_not_an_error(self, tmp_path):
        assert vs.migrate_legacy_db()["migrated"] == 0

    def test_an_empty_legacy_file_does_not_raise(self, tmp_path):
        (tmp_path / "vectors.db").write_bytes(b"")

        assert vs.migrate_legacy_db()["migrated"] == 0


class TestShardKeyCollisions:
    """
    Sanitising a name for the filesystem is lossy, and a collision here does not corrupt a file —
    it merges two members' vectors into one shard and leaks their libraries into each other's
    search results. That is the boundary sharding exists to enforce.
    """

    def test_names_that_sanitise_the_same_get_different_shards(self):
        assert vs.shard_key("Mr.Paras") != vs.shard_key("Mr_Paras")

    def test_a_clean_name_is_left_untouched(self):
        # Existing shard files on deployed boards must keep their names.
        assert vs.shard_key("Paras") == "Paras"
        assert vs.shard_key("phase1test") == "phase1test"

    def test_traversal_is_still_neutralised(self):
        key = vs.shard_key("../../etc/passwd")
        assert "/" not in key and ".." not in key

    def test_the_key_is_stable_across_calls(self):
        assert vs.shard_key("Mr.Paras") == vs.shard_key("Mr.Paras")

    def test_routing_and_lookup_agree_for_an_awkward_name(self):
        path = f"{vs.settings.nas_root}/personal/Mr.Paras/a.jpg".replace("//", "/")
        assert vs.shard_for_path(path) in vs.shards_for_user("Mr.Paras")


class TestTwoModelsPerFile:
    """
    A file has one vector per model. Under the original `PRIMARY KEY (rel_path)` a second model's
    vector silently *replaced* the first — no error, no conflict, the text vector simply gone and
    search quietly worse. These tests reproduce that.
    """

    OTHER = "mobileclip_s0"

    def test_a_second_model_does_not_evict_the_first(self):
        path = "photos/a.jpg"
        text_vec, image_vec = unit(31), unit(32)
        vs.upsert(path, text_vec, model=MODEL, content_key="k")
        vs.upsert(path, image_vec, model=self.OTHER, content_key="k")

        assert vs.search(text_vec, model=MODEL, limit=1)[0].rel_path == path
        assert vs.search(image_vec, model=self.OTHER, limit=1)[0].rel_path == path

    def test_each_model_sees_only_its_own_vectors(self):
        vs.upsert("a.jpg", unit(33), model=MODEL, content_key="k")
        vs.upsert("b.jpg", unit(34), model=self.OTHER, content_key="k")

        assert [h.rel_path for h in vs.search(unit(33), model=MODEL, limit=5)] == ["a.jpg"]
        assert [h.rel_path for h in vs.search(unit(34), model=self.OTHER, limit=5)] == ["b.jpg"]

    def test_re_embedding_the_same_file_and_model_still_replaces(self):
        """The composite key must not turn updates into duplicates."""
        path = "photos/a.jpg"
        vs.upsert(path, unit(35), model=MODEL, content_key="v1")
        vs.upsert(path, unit(36), model=MODEL, content_key="v2")

        assert vs.stats()["count"] == 1

    def test_deleting_a_file_removes_every_model(self):
        path = "photos/a.jpg"
        vs.upsert(path, unit(37), model=MODEL, content_key="k")
        vs.upsert(path, unit(38), model=self.OTHER, content_key="k")

        vs.delete([path])

        assert vs.stats()["count"] == 0

    def test_widths_may_differ_between_models(self):
        """CLIP is 512-dim, bge is 384 — both must coexist in one shard."""
        wide = np.random.default_rng(9).standard_normal(512).astype(np.float32)
        wide /= np.linalg.norm(wide)
        vs.upsert("a.jpg", unit(39), model=MODEL, content_key="k")
        vs.upsert("a.jpg", wide, model=self.OTHER, content_key="k")

        assert vs.search(wide, model=self.OTHER, limit=1)[0].rel_path == "a.jpg"
        assert vs.search(unit(39), model=MODEL, limit=1)[0].rel_path == "a.jpg"


class TestPrimaryKeyMigration:
    def test_an_old_single_key_table_is_widened_in_place(self, tmp_path, monkeypatch):
        """A board upgrading must keep its vectors and gain room for a second model."""
        import sqlite3
        monkeypatch.setattr(vs.settings, "data_dir", tmp_path)
        vs._reset_for_tests()
        db = vs._db_path()
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE vectors (rel_path TEXT PRIMARY KEY, dim INTEGER NOT NULL, "
            "dtype TEXT NOT NULL, vector BLOB NOT NULL, model TEXT NOT NULL, "
            "content_key TEXT NOT NULL)"
        )
        v = unit(40)
        conn.execute("INSERT INTO vectors VALUES (?, ?, ?, ?, ?, ?)",
                     ("a.jpg", vs.DIM, "float32", v.tobytes(), MODEL, "k"))
        conn.commit()
        conn.close()

        vs.init_db()  # triggers the migration through _create_schema

        assert vs.search(v, model=MODEL, limit=1)[0].rel_path == "a.jpg"
        vs.upsert("a.jpg", unit(41), model="mobileclip_s0", content_key="k")
        assert vs.stats()["count"] == 2, "the second model must coexist, not replace"


class TestDeletedMediaIsNotIndexed:
    """
    `media.blobs` is the content table; `entries.deleted` is where deletion lives. The diff used to
    read blobs alone, so a photo the family deleted kept its vector and stayed findable in semantic
    search — the file was gone from the app and still turned up in results.
    """

    def _media(self, tmp_path, rows):
        """rows: (rel_path, content_hash, deleted)"""
        import sqlite3
        db = tmp_path / "media.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE blobs (id INTEGER PRIMARY KEY, rel_path TEXT UNIQUE, "
                     "content_hash TEXT NOT NULL)")
        conn.execute("CREATE TABLE entries (id INTEGER PRIMARY KEY, blob_id INTEGER, "
                     "deleted INTEGER NOT NULL DEFAULT 0)")
        for i, (rel, h, dele) in enumerate(rows, start=1):
            conn.execute("INSERT INTO blobs VALUES (?,?,?)", (i, rel, h))
            conn.execute("INSERT INTO entries (blob_id, deleted) VALUES (?,?)", (i, dele))
        conn.commit(); conn.close()
        return db

    def test_a_deleted_file_is_not_offered_for_embedding(self, tmp_path):
        media = self._media(tmp_path, [("/p/live.jpg", "h1", 0), ("/p/gone.jpg", "h2", 1)])

        to_embed, _ = vs.rebuild_required_from_db(media, MODEL)

        assert to_embed == ["/p/live.jpg"]

    def test_an_existing_vector_for_a_deleted_file_is_purged(self, tmp_path):
        media = self._media(tmp_path, [("/p/gone.jpg", "h2", 1)])
        vs.upsert("/p/gone.jpg", unit(50), model=MODEL, content_key="h2")

        _, to_delete = vs.rebuild_required_from_db(media, MODEL)

        assert to_delete == ["/p/gone.jpg"]

    def test_a_file_with_one_live_entry_among_deleted_ones_still_counts(self, tmp_path):
        """Several entries can share a blob; one surviving entry means the content is still there."""
        import sqlite3
        media = self._media(tmp_path, [("/p/a.jpg", "h1", 1)])
        conn = sqlite3.connect(media)
        conn.execute("INSERT INTO entries (blob_id, deleted) VALUES (1, 0)")
        conn.commit(); conn.close()

        to_embed, to_delete = vs.rebuild_required_from_db(media, MODEL)

        assert to_embed == ["/p/a.jpg"]
        assert to_delete == []

    def test_a_blob_missing_entirely_still_purges(self, tmp_path):
        media = self._media(tmp_path, [("/p/live.jpg", "h1", 0)])
        vs.upsert("/p/vanished.jpg", unit(51), model=MODEL, content_key="h9")

        _, to_delete = vs.rebuild_required_from_db(media, MODEL)

        assert to_delete == ["/p/vanished.jpg"]
