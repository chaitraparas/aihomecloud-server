"""
Embedding *pixels*, so photos can be found by what is in them.

Text embedding of file paths cannot answer "beach holiday" or "kids playing", and measurement on
the real library says why: a third of files are camera exports like `IMG_20260428_083411.jpg`, and
`original_name` is byte-identical to `filename` for every media row. There is no text to match.
OCR enrichment fixed documents and screenshots; nothing but the image itself can fix photos.

**MobileCLIP s0**, chosen over `clip-vit-base-patch32` on size (54.6 MB against 153 MB) and
validated on the production board against ground truth derived from the OCR index — AUC 0.741 and
precision@10 0.80 for "a screenshot" against a 0.30 base rate, and 0.411 (deliberately *below*
chance, the correct direction) for "a photo of a natural scene".

CLIP-family models put images and text in one shared space, so a text query can be compared
directly against image vectors. Two consequences that shape this module:

* **Two encoders are needed.** Vision at index time, text at query time. `bge-small` cannot be
  reused for the query — its space is unrelated, and comparing across them is meaningless.
* **Their similarity scales differ.** CLIP cosines sit around 0.1–0.3 where bge sits at 0.4–0.7,
  which is why the caller fuses by *rank* and never by raw score.

Measured on the boards:

| | RSS | speed |
|---|---|---|
| vision fp32 | 77 MB | 486 ms/image |
| vision int8 | 47 MB | 646 ms/image |
| text int8 | 27 MB | — |

fp32 vision is the default: int8 is a memory-for-latency trade here, not a win, because this CPU
has no `FEAT_DotProd` and must unpack int8 before multiplying. Observed on two unrelated models
now, so treat it as a property of the hardware. The vision encoder is only resident while indexing
and is released afterwards; the text encoder is small and stays warm to serve queries.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Sequence

import numpy as np

from .config import settings

logger = logging.getLogger("aihomecloud.image_embedding")

MODEL_NAME = "mobileclip_s0"
DIM = 512

# The model's own preprocessor config: shortest edge to 256, centre crop, and **do_normalize is
# false** — no ImageNet mean/std subtraction. Applying it out of habit silently degrades every
# embedding without raising anything.
IMAGE_SIZE = 256
MAX_TOKENS = 77          # CLIP's fixed context length; the text encoder expects exactly this

_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".heic", ".heif"}


def is_image(rel_path: str) -> bool:
    """Only images go to the vision encoder — pointing it at an `.mkv` wastes hours."""
    return Path(rel_path).suffix.lower() in _IMAGE_SUFFIXES


def model_dir(name: str = MODEL_NAME) -> Path:
    return settings.data_dir / "models" / name


def available(name: str = MODEL_NAME) -> bool:
    """
    Runtime **and** files. A board without these serves everything else and simply has no image
    search — the same contract as text embedding.
    """
    try:
        import onnxruntime  # noqa: F401,PLC0415
        from tokenizers import Tokenizer  # noqa: F401,PLC0415
    except ImportError:
        return False
    d = model_dir(name)
    return (d / "vision_model.onnx").is_file() and (d / "text_model.onnx").is_file() \
        and (d / "tokenizer.json").is_file()


def _unit(v: np.ndarray) -> np.ndarray:
    n = np.clip(np.linalg.norm(v, axis=-1, keepdims=True), 1e-9, None)
    return (v / n).astype(np.float32)


class ImageEmbeddingProvider:
    """
    Both halves of MobileCLIP, each loaded on first use and independently releasable.

    Splitting the lifetimes matters on a 937 MB board: the vision encoder is the expensive one
    (77 MB) and is only needed during an indexing run, while the text encoder (27 MB) must stay
    available to answer queries.
    """

    def __init__(self, directory: Path | None = None, threads: int | None = None):
        self._dir = directory or model_dir()
        self._threads = threads if threads is not None else getattr(settings, "embedding_threads", 4)
        self._lock = threading.Lock()
        self._vision = None
        self._text = None
        self._tokenizer = None
        self._vision_input = ""
        self._text_input = ""

    @property
    def model_name(self) -> str:
        return MODEL_NAME

    @property
    def dim(self) -> int:
        return DIM

    def _session(self, filename: str):
        import onnxruntime as ort  # noqa: PLC0415 — lazy, so a board without it still boots

        opts = ort.SessionOptions()
        # Not every core: the board serves files while this runs, and pinning all of them during a
        # multi-hour build is what turns indexing into a thermal problem.
        opts.intra_op_num_threads = self._threads
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        return ort.InferenceSession(
            str(self._dir / filename), opts, providers=["CPUExecutionProvider"]
        )

    # --- vision ------------------------------------------------------------

    def _ensure_vision(self) -> None:
        if self._vision is not None:
            return
        with self._lock:
            if self._vision is not None:
                return
            s = self._session("vision_model.onnx")
            self._vision_input = s.get_inputs()[0].name
            self._vision = s
            logger.info("image_vision_loaded dir=%s", self._dir)

    def release_vision(self) -> None:
        """Drop the vision encoder once indexing is done — 77 MB back on a 937 MB board."""
        with self._lock:
            if self._vision is not None:
                self._vision = None
                logger.info("image_vision_released")

    def preprocess(self, path: Path | str) -> np.ndarray:
        from PIL import Image  # noqa: PLC0415

        with Image.open(path) as im:
            im = im.convert("RGB")
            w, h = im.size
            scale = IMAGE_SIZE / min(w, h)
            im = im.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.BILINEAR)
            w, h = im.size
            left, top = (w - IMAGE_SIZE) // 2, (h - IMAGE_SIZE) // 2
            im = im.crop((left, top, left + IMAGE_SIZE, top + IMAGE_SIZE))
            arr = np.asarray(im, dtype=np.float32) / 255.0
        return arr.transpose(2, 0, 1)[None, ...]

    def embed_image(self, path: Path | str) -> np.ndarray:
        self._ensure_vision()
        out = self._vision.run(None, {self._vision_input: self.preprocess(path)})[0]
        return _unit(out[0])

    def embed_images(self, paths: Sequence[Path | str]) -> tuple[list[str], np.ndarray]:
        """
        Embed what can be read; skip what cannot, and say which.

        A single unreadable or truncated photo must not abandon a multi-hour indexing run, and the
        caller needs to know which paths actually produced a vector so it does not store a wrong
        path against a good vector.
        """
        ok: list[str] = []
        vecs: list[np.ndarray] = []
        for p in paths:
            try:
                vecs.append(self.embed_image(p))
                ok.append(str(p))
            except Exception as exc:  # corrupt file, unsupported mode, truncated download
                logger.warning("image_embed_failed path=%s error=%s", p, exc)
        if not vecs:
            return [], np.empty((0, DIM), dtype=np.float32)
        return ok, np.vstack(vecs).astype(np.float32)

    # --- text --------------------------------------------------------------

    def _ensure_text(self) -> None:
        if self._text is not None:
            return
        with self._lock:
            if self._text is not None:
                return
            from tokenizers import Tokenizer  # noqa: PLC0415

            s = self._session("text_model.onnx")
            tok = Tokenizer.from_file(str(self._dir / "tokenizer.json"))
            # CLIP's text tower is fixed-width: it wants exactly MAX_TOKENS, padded, every time.
            tok.enable_truncation(max_length=MAX_TOKENS)
            tok.enable_padding(length=MAX_TOKENS)
            self._text_input = s.get_inputs()[0].name
            self._tokenizer = tok
            self._text = s
            logger.info("image_text_loaded dir=%s", self._dir)

    def embed_query(self, text: str) -> np.ndarray:
        self._ensure_text()
        ids = np.array([self._tokenizer.encode(text).ids], dtype=np.int64)
        out = self._text.run(None, {self._text_input: ids})[0]
        return _unit(out[0])
