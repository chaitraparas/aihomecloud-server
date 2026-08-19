"""
Turning text into vectors, behind an interface (Phase 4 milestone 1).

The interface exists because the model choice is not settled forever and the runtime is heavy.
`vector_store` should never import onnxruntime, tests should never need it, and swapping the
model later should not touch anything but this file.

## Which model, and why not the one the architecture doc names

Measured on the fleet 2026-08-05 (`docs/PHASE4_EMBEDDING_SPIKE_2026-08-05.md`):

    all-MiniLM-L6-v2   (the doc's)   102.6 ms/query   MRR 0.947   167 min to index 100k
    bge-small-en-v1.5                 22.6 ms/query   MRR 0.956    24 min

`bge-small-en-v1.5` is faster *and* scores higher, so there is no trade-off to weigh. It has
twelve layers against the doc's six — the gap is a property of that particular ONNX export, not
of ARM, and was verified order-independent.

`e5-small-v2` scored better still (MRR 1.000 on the test corpus) and is deliberately **not**
chosen. It requires `query:` and `passage:` prefixes on both sides and degrades to near-useless
without them — during the spike it initially looked broken for exactly that reason. On an
indexing pipeline that will be maintained for years, a model that fails loudly beats one that
fails quietly by a fraction of a point.

## The dependency is optional on purpose

`onnxruntime` and `tokenizers` are ~200 MB installed and are **not** in `requirements.txt`. This
module imports them lazily and reports [available] as False when they are missing, so a backend
without them starts, serves, and simply does not offer semantic search. Deciding to put 200 MB on
every board is a deployment decision, not something a module import should make by failing.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence

import numpy as np

from .config import settings

logger = logging.getLogger("aihomecloud.embedding")

@dataclass(frozen=True)
class ModelSpec:
    """
    One embedding model. Everything that differs between them lives here so the rest of the
    codebase never branches on model identity.

    `dim` is the reason this is a registry rather than a constant: the models differ (384 / 768 /
    1024), and a stored vector of the wrong width produces meaningless similarity rather than an
    error. Switching a board's model therefore forces a full re-embed of that board — vectors
    from two models are not comparable and are never mixed.

    `query_prefix` is per-model and asymmetric. bge prefixes the query side only; e5 requires
    *both* sides prefixed and degrades to near-useless if either is missed, which is why e5 is
    not in this registry despite scoring best in testing.
    """
    name: str
    dim: int
    repo: str
    query_prefix: str = ""
    doc_prefix: str = ""


MODELS: dict[str, ModelSpec] = {
    # Measured on this fleet 2026-08-05 — the default, and the only one measured end to end.
    # 22.6 ms/query and MRR 0.956 on the slowest board.
    "bge-small-en-v1.5": ModelSpec(
        name="bge-small-en-v1.5",
        dim=384,
        repo="Xenova/bge-small-en-v1.5",
        query_prefix="Represent this sentence for searching relevant passages: ",
    ),
    # Larger tiers, MEASURED 2026-08-05. The result is that tiering should key on CPU, not RAM:
    #
    #                 ROCK Pi 4A (6c ARM)      x86 thin client (4c)
    #   small  384      57.3 ms  OK              23.3 ms  OK
    #   base   768     145.3 ms  over            55.9 ms  OK
    #   large 1024     380.6 ms  over           153.2 ms  over
    #
    # The ROCK Pi has 3.8 GB — ample RAM for base's 307 MB index — and still misses the 75 ms
    # budget by 2x. Memory was never the binding constraint; query embedding was. `large` fails
    # on every board in this fleet.
    #
    # So: `small` everywhere, `base` only on x86-class hardware, `large` on nothing we own.
    "bge-base-en-v1.5": ModelSpec(
        name="bge-base-en-v1.5",
        dim=768,
        repo="Xenova/bge-base-en-v1.5",
        query_prefix="Represent this sentence for searching relevant passages: ",
    ),
    "bge-large-en-v1.5": ModelSpec(
        name="bge-large-en-v1.5",
        dim=1024,
        repo="Xenova/bge-large-en-v1.5",
        query_prefix="Represent this sentence for searching relevant passages: ",
    ),
    # Multilingual option, for a household that names files in Devanagari or transliterated
    # Hindi. **Not the default, on measured grounds:**
    #
    #                    RSS      query     MRR (8-doc mixed-script corpus)
    #   bge-small-en    +69 MB   23.1 ms    1.000
    #   multilingual   +383 MB   12.3 ms    1.000
    #
    # It is twice as fast and uses five and a half times the memory — 383 MB on a board with
    # 937 MB total is the deciding number. And the quality case for switching is *unproven*: both
    # models saturate that corpus at MRR 1.000, so it cannot separate them.
    #
    # The concern behind it is real and unresolved: bge-small-en scores Devanagari and
    # transliterated filenames near-identically regardless of relevance — for "diwali festival",
    # a Devanagari *beach* photo outranked a Devanagari *Diwali* one. If this household's naming
    # turns out to be substantially non-English, switch to this and re-measure on a corpus large
    # enough to tell them apart.
    "paraphrase-multilingual-MiniLM-L12-v2": ModelSpec(
        name="paraphrase-multilingual-MiniLM-L12-v2",
        dim=384,
        repo="Xenova/paraphrase-multilingual-MiniLM-L12-v2",
    ),
    # Deliberately included so the earlier finding is not lost: all-MiniLM-L6-v2 is what the
    # architecture doc named, and on ARM it is 4.5x SLOWER than bge-small (102.6 ms vs 22.6) for
    # a slightly worse MRR. There is no tier on this fleet where it is the right choice. It stays
    # selectable only so a board already indexed with it can keep working until re-embedded.
    "all-MiniLM-L6-v2": ModelSpec(
        name="all-MiniLM-L6-v2", dim=384, repo="Xenova/all-MiniLM-L6-v2",
    ),
}

DEFAULT_MODEL = "bge-small-en-v1.5"

_MAX_TOKENS = 256


def spec_for(model_name: str | None = None) -> ModelSpec:
    """
    The spec for [model_name], or the configured board default.

    An unknown name falls back to the default with a warning rather than raising: a typo in an
    environment variable should not stop the backend serving files, and semantic search degrading
    to the default model is a far smaller problem than the box not starting.
    """
    name = model_name or getattr(settings, "embedding_model", DEFAULT_MODEL)
    spec = MODELS.get(name)
    if spec is None:
        logger.warning("unknown embedding_model=%r, falling back to %s", name, DEFAULT_MODEL)
        return MODELS[DEFAULT_MODEL]
    return spec


# Retained for callers that predate the registry. Prefer spec_for().
MODEL_NAME = DEFAULT_MODEL
DIM = MODELS[DEFAULT_MODEL].dim


class EmbeddingProvider(Protocol):
    """What `vector_store`'s callers need. Deliberately small enough to fake in a test."""

    @property
    def model_name(self) -> str: ...

    def embed_documents(self, texts: Sequence[str]) -> np.ndarray: ...

    def embed_query(self, text: str) -> np.ndarray: ...


class OnnxEmbeddingProvider:
    """
    Any model in [MODELS], int8 ONNX, via onnxruntime.

    The session is built once and reused: measured on the Cubie, load costs ~77 MB of RSS and a
    one-off graph initialisation that would otherwise be paid per call.
    """

    def __init__(
        self,
        model_dir: Path | None = None,
        threads: int | None = None,
        spec: ModelSpec | None = None,
    ):
        self.spec = spec or spec_for()
        self._dir = model_dir or model_dir_for(self.spec)
        self._threads = threads if threads is not None else getattr(settings, "embedding_threads", 4)
        self._lock = threading.Lock()
        self._session = None
        self._tokenizer = None
        self._input_names: set[str] = set()

    @property
    def model_name(self) -> str:
        return self.spec.name

    @property
    def dim(self) -> int:
        return self.spec.dim

    def _ensure_loaded(self) -> None:
        if self._session is not None:
            return
        with self._lock:
            if self._session is not None:
                return
            import onnxruntime as ort  # noqa: PLC0415 — deliberately lazy, see module docstring
            from tokenizers import Tokenizer  # noqa: PLC0415

            opts = ort.SessionOptions()
            # Not every core: these boards serve files at the same time, and pinning all of them
            # during a multi-hour index build is what turns this into a thermal problem.
            opts.intra_op_num_threads = self._threads
            opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

            session = ort.InferenceSession(
                str(self._dir / "model.onnx"), opts, providers=["CPUExecutionProvider"]
            )
            tokenizer = Tokenizer.from_file(str(self._dir / "tokenizer.json"))
            tokenizer.enable_truncation(max_length=_MAX_TOKENS)

            self._input_names = {i.name for i in session.get_inputs()}
            self._tokenizer = tokenizer
            self._session = session
            logger.info("embedding_model_loaded model=%s dim=%d dir=%s",
                        self.spec.name, self.spec.dim, self._dir)

    def _run(self, texts: Sequence[str]) -> np.ndarray:
        self._ensure_loaded()
        encoded = [self._tokenizer.encode(t) for t in texts]
        width = max(len(e.ids) for e in encoded)
        ids = np.array([e.ids + [0] * (width - len(e.ids)) for e in encoded], dtype=np.int64)
        mask = np.array(
            [e.attention_mask + [0] * (width - len(e.attention_mask)) for e in encoded],
            dtype=np.int64,
        )
        feed = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self._input_names:
            feed["token_type_ids"] = np.zeros_like(ids)

        hidden = self._session.run(None, feed)[0]
        # Mean-pool over real tokens only. Including padding would drag every short filename
        # toward the same vector, which looks like poor retrieval rather than a pooling bug.
        m = mask[..., None].astype(np.float32)
        pooled = (hidden * m).sum(1) / np.clip(m.sum(1), 1e-9, None)
        norms = np.clip(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-9, None)
        return (pooled / norms).astype(np.float32)

    def embed_documents(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.empty((0, self.spec.dim), dtype=np.float32)
        prefix = self.spec.doc_prefix
        return self._run([prefix + t for t in texts] if prefix else list(texts))

    def embed_query(self, text: str) -> np.ndarray:
        return self._run([self.spec.query_prefix + text])[0]


def model_dir_for(spec: ModelSpec | None = None) -> Path:
    """Where this board keeps a model's files. Per model, so switching does not overwrite."""
    return settings.data_dir / "models" / (spec or spec_for()).name


def available(model_dir: Path | None = None, spec: ModelSpec | None = None) -> bool:
    """
    Whether semantic search can run here at all: runtime installed *and* this model present.

    Both halves matter and fail differently — a board can have the 200 MB runtime and no model
    files, or the files and no runtime. Checked per model, because a board that switched models
    has the old files and not the new ones. Callers should treat False as "do not offer the
    feature", not as an error.
    """
    try:
        import onnxruntime  # noqa: F401,PLC0415
        import tokenizers  # noqa: F401,PLC0415
    except ImportError:
        return False
    d = model_dir or model_dir_for(spec)
    return (d / "model.onnx").is_file() and (d / "tokenizer.json").is_file()


def humanise_path(rel_path: str) -> str:
    """
    Turn a stored path into something worth embedding.

    `Photos/2024/Goa trip/IMG_20240115_beach_sunset.jpg` carries real signal — the folder names
    are how people describe their own libraries — but separators, extensions and the NAS root are
    noise.

    Measured on a real board (128 files): paths are 2–7 levels deep and humanise to a median of
    8 words. **A third of filenames are opaque camera exports** like `IMG_20240115_123456`, which
    contribute nothing themselves — for those, retrieval rests entirely on the folder names, which
    is a reason to embed richer metadata (capture date, detected content) later rather than to
    change this.
    """
    text = rel_path
    # Strip the NAS root. It is identical on every file, so it contributes nothing to ranking
    # while occupying real estate in a short string — measured on a real board, paths humanise to
    # a median of 8 words, of which the root was 2. A quarter of the signal spent on a constant.
    root = str(getattr(settings, "nas_root", "")).strip("/")
    if root:
        prefix = "/" + root
        if text.startswith(prefix + "/"):
            text = text[len(prefix) + 1:]
        elif text.startswith(root + "/"):
            text = text[len(root) + 1:]

    without_ext = text.rsplit(".", 1)[0] if "." in text.rsplit("/", 1)[-1] else text
    return " ".join(
        without_ext.replace("/", " ").replace("_", " ").replace("-", " ").split()
    )
