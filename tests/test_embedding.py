"""
The embedding provider interface.

onnxruntime is not a backend dependency, so nothing here loads a model. What is tested is the
part that runs on every board regardless: whether the feature reports itself available honestly,
and the path-to-text transformation that the retrieval-quality numbers were measured against.
"""

import numpy as np
import pytest

from app import embedding


class TestAvailability:
    def test_reports_unavailable_when_the_model_files_are_missing(self, tmp_path):
        # The runtime may or may not be installed on the machine running these tests; either way
        # an empty directory means the feature cannot work.
        assert embedding.available(model_dir=tmp_path) is False

    def test_requires_both_files_not_just_one(self, tmp_path):
        (tmp_path / "model.onnx").write_bytes(b"not really a model")
        # tokenizer.json deliberately absent — a half-downloaded model must not look ready.
        assert embedding.available(model_dir=tmp_path) is False


class TestHumanisePath:
    """
    These cases are the exact transformation the retrieval-quality comparison scored against
    (MRR 0.956 for bge). Changing the behaviour here invalidates those numbers.
    """

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Photos/2024/Goa trip/IMG_20240115_beach_sunset.jpg",
             "Photos 2024 Goa trip IMG 20240115 beach sunset"),
            ("Documents/Tax/form_16_salary_certificate.pdf",
             "Documents Tax form 16 salary certificate"),
            ("Videos/Baby/first-steps-walking.mp4", "Videos Baby first steps walking"),
        ],
    )
    def test_folders_and_separators_become_words(self, raw, expected):
        assert embedding.humanise_path(raw) == expected

    def test_a_directory_with_a_dot_keeps_its_last_segment(self):
        # Only a real extension should be stripped. "v1.2" is part of the folder name, and
        # chopping at the last dot in the whole path would eat it.
        assert embedding.humanise_path("Archive/v1.2/notes") == "Archive v1.2 notes"

    def test_a_file_with_no_extension_is_left_alone(self):
        assert embedding.humanise_path("Documents/README") == "Documents README"


class TestProviderContract:
    """A fake standing in for the real provider — the shape callers depend on."""

    class FakeProvider:
        model_name = "fake"

        def embed_documents(self, texts):
            return np.ones((len(texts), embedding.DIM), dtype=np.float32) / np.sqrt(embedding.DIM)

        def embed_query(self, text):
            return np.ones(embedding.DIM, dtype=np.float32) / np.sqrt(embedding.DIM)

    def test_a_fake_satisfies_the_protocol(self):
        provider: embedding.EmbeddingProvider = self.FakeProvider()

        docs = provider.embed_documents(["a", "b"])
        query = provider.embed_query("a")

        assert docs.shape == (2, embedding.DIM)
        assert query.shape == (embedding.DIM,)

    def test_empty_input_yields_an_empty_matrix_not_an_error(self):
        # The indexer will hand this an empty batch at the end of a scan.
        out = embedding.OnnxEmbeddingProvider().embed_documents([])
        assert out.shape == (0, embedding.DIM)


class TestModelRegistry:
    def test_the_default_is_bge_small(self):
        assert embedding.spec_for().name == "bge-small-en-v1.5"
        assert embedding.spec_for().dim == 384

    def test_bge_prefixes_the_query_side_only(self):
        # Prefixing documents too is the e5 convention; applying it to bge would quietly degrade
        # retrieval rather than fail.
        spec = embedding.spec_for("bge-small-en-v1.5")
        assert "searching relevant passages" in spec.query_prefix
        assert spec.doc_prefix == ""

    @pytest.mark.parametrize("name,dim", [
        ("bge-small-en-v1.5", 384), ("bge-base-en-v1.5", 768), ("bge-large-en-v1.5", 1024),
    ])
    def test_each_tier_declares_its_own_width(self, name, dim):
        # The whole reason this is a registry: a wrong width produces meaningless similarity
        # rather than an error.
        assert embedding.spec_for(name).dim == dim

    def test_an_unknown_model_falls_back_rather_than_crashing(self):
        # A typo in an env var must not stop the backend serving files.
        assert embedding.spec_for("no-such-model").name == embedding.DEFAULT_MODEL

    def test_minilm_is_still_selectable_but_is_not_the_default(self):
        # Kept only so a board already indexed with it keeps working; it measured 4.5x slower
        # than bge-small on ARM for slightly worse retrieval.
        assert "all-MiniLM-L6-v2" in embedding.MODELS
        assert embedding.DEFAULT_MODEL != "all-MiniLM-L6-v2"

    def test_each_model_gets_its_own_directory(self, tmp_path, monkeypatch):
        # Switching models must not overwrite the previous model's files.
        monkeypatch.setattr(embedding.settings, "data_dir", tmp_path)
        small = embedding.model_dir_for(embedding.spec_for("bge-small-en-v1.5"))
        base = embedding.model_dir_for(embedding.spec_for("bge-base-en-v1.5"))
        assert small != base


class TestHumanisePathStripsTheRoot:
    """
    Every file shares the NAS root, so it ranks nothing and dilutes a short string. Measured on a
    real board: median 8 words per path, of which the root was 2.
    """

    def test_the_nas_root_is_removed(self, monkeypatch):
        monkeypatch.setattr(embedding.settings, "nas_root", "/srv/nas")
        assert embedding.humanise_path("/srv/nas/family/Videos/beach_day.mp4") == "family Videos beach day"

    def test_a_path_without_the_root_is_untouched(self, monkeypatch):
        monkeypatch.setattr(embedding.settings, "nas_root", "/srv/nas")
        assert embedding.humanise_path("Photos/holiday_snap.jpg") == "Photos holiday snap"

    def test_runs_of_separators_do_not_leave_blank_words(self, monkeypatch):
        monkeypatch.setattr(embedding.settings, "nas_root", "/srv/nas")
        assert embedding.humanise_path("/srv/nas/a__b--c/d.jpg") == "a b c d"
