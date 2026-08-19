"""
Image embeddings, and fusing them with text results.

The failure guarded against here is a *units* bug that looks like a quality problem: CLIP and bge
similarities are on different scales, so merging by raw score buries every image match under every
text match and reads as "image search doesn't work".
"""

from collections import namedtuple

import numpy as np
import pytest

from app import image_embedding as ie
from app.routes.file_routes import _fuse_by_rank

Hit = namedtuple("Hit", "rel_path score")


class TestRankFusion:
    def test_a_file_found_by_both_outranks_one_found_by_either(self):
        text = [Hit("both.jpg", 0.7), Hit("text-only.jpg", 0.69)]
        image = [Hit("image-only.jpg", 0.3), Hit("both.jpg", 0.29)]

        fused = _fuse_by_rank(text, image, limit=5)

        assert fused[0][0] == "both.jpg"

    def test_image_results_are_not_buried_by_higher_text_scores(self):
        """
        The units bug, stated as a test. Every text score here beats every image score, yet the
        top image result must still place — it is rank 1 of its own list.
        """
        text = [Hit(f"t{i}.jpg", 0.9 - i * 0.01) for i in range(10)]
        image = [Hit("beach.jpg", 0.21), Hit("sea.jpg", 0.19)]

        fused = _fuse_by_rank(text, image, limit=5)
        paths = [p for p, _ in fused]

        assert "beach.jpg" in paths
        assert paths.index("beach.jpg") <= 1, "a rank-1 image hit belongs near the top"

    def test_order_within_a_single_list_is_preserved(self):
        text = [Hit("a", 0.9), Hit("b", 0.8), Hit("c", 0.7)]

        assert [p for p, _ in _fuse_by_rank(text, [], limit=3)] == ["a", "b", "c"]

    def test_limit_is_respected(self):
        text = [Hit(f"t{i}", 0.5) for i in range(20)]
        image = [Hit(f"i{i}", 0.2) for i in range(20)]

        assert len(_fuse_by_rank(text, image, limit=7)) == 7

    def test_no_image_hits_degrades_to_the_text_ranking(self):
        text = [Hit("a", 0.9), Hit("b", 0.8)]

        assert [p for p, _ in _fuse_by_rank(text, [], limit=5)] == ["a", "b"]

    def test_both_empty(self):
        assert _fuse_by_rank([], [], limit=5) == []

    def test_duplicates_are_merged_not_repeated(self):
        text = [Hit("same.jpg", 0.9)]
        image = [Hit("same.jpg", 0.2)]

        fused = _fuse_by_rank(text, image, limit=5)

        assert len(fused) == 1


class TestImageFileSelection:
    @pytest.mark.parametrize("path", [
        "/family/Photos/a.jpg", "/family/Photos/b.JPEG", "/p/c.png",
        "/p/d.webp", "/p/e.heic",
    ])
    def test_images_are_selected(self, path):
        assert ie.is_image(path)

    @pytest.mark.parametrize("path", [
        "/entertainment/Anime/ep1.mkv", "/docs/a.pdf", "/p/b.mp4", "/p/c.txt", "/p/noext",
    ])
    def test_everything_else_is_skipped(self, path):
        """Handing an .mkv to the vision encoder costs half a second and yields nothing useful."""
        assert not ie.is_image(path)


class TestPreprocessing:
    def _provider(self, tmp_path):
        return ie.ImageEmbeddingProvider(directory=tmp_path)

    def test_output_shape_is_what_the_model_expects(self, tmp_path):
        from PIL import Image

        p = tmp_path / "a.jpg"
        Image.new("RGB", (640, 480), (120, 30, 200)).save(p)

        arr = self._provider(tmp_path).preprocess(p)

        assert arr.shape == (1, 3, ie.IMAGE_SIZE, ie.IMAGE_SIZE)
        assert arr.dtype == np.float32

    def test_values_are_scaled_but_not_mean_normalised(self, tmp_path):
        """
        `do_normalize` is false for this model. Applying ImageNet mean/std out of habit would push
        values negative and silently degrade every embedding without raising anything.
        """
        from PIL import Image

        p = tmp_path / "white.png"
        Image.new("RGB", (300, 300), (255, 255, 255)).save(p)

        arr = self._provider(tmp_path).preprocess(p)

        assert arr.min() >= 0.0 and arr.max() <= 1.0
        assert np.allclose(arr, 1.0), "pure white must map to 1.0, not a normalised offset"

    def test_a_portrait_image_is_centre_cropped_square(self, tmp_path):
        from PIL import Image

        p = tmp_path / "tall.jpg"
        Image.new("RGB", (200, 900), (10, 200, 10)).save(p)

        assert self._provider(tmp_path).preprocess(p).shape[2:] == (ie.IMAGE_SIZE, ie.IMAGE_SIZE)


class TestAvailability:
    def test_missing_model_files_report_unavailable(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ie.settings, "data_dir", tmp_path)

        assert ie.available() is False

    def test_a_partial_model_directory_is_not_available(self, tmp_path, monkeypatch):
        """Half a model must not read as installed — that fails at query time instead of startup."""
        monkeypatch.setattr(ie.settings, "data_dir", tmp_path)
        d = tmp_path / "models" / ie.MODEL_NAME
        d.mkdir(parents=True)
        (d / "vision_model.onnx").write_bytes(b"x")

        assert ie.available() is False
