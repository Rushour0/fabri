from __future__ import annotations

import logging
import os
from importlib.util import find_spec
from pathlib import Path
from typing import TYPE_CHECKING

# Mute HuggingFace / sentence-transformers chatter BEFORE the import. The
# `Loading weights` tqdm bar and the "unauthenticated requests to the HF Hub"
# warning otherwise leak straight to stderr on every `fabri run`.
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

logging.getLogger("sentence_transformers").setLevel(logging.WARNING)
logging.getLogger("transformers").setLevel(logging.ERROR)
logging.getLogger("huggingface_hub").setLevel(logging.ERROR)

logger = logging.getLogger("fabri")

MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_DIM = 384

_model: SentenceTransformer | None = None

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer


_MISSING_EMBEDDINGS_MESSAGE = (
    "sentence-transformers is not installed — `pip install 'fabri[embeddings]'` "
    "to enable memory retrieval/learning"
)


def embeddings_available() -> bool:
    """Return whether the optional sentence-transformers package is installed."""
    return find_spec("sentence_transformers") is not None


def _model_cache_dir() -> Path:
    hf_home = os.environ.get("HF_HOME") or os.environ.get("HUGGINGFACE_HUB_CACHE")
    base = Path(hf_home) if hf_home else Path.home() / ".cache" / "huggingface"
    return base / "hub" / f"models--{MODEL_NAME.replace('/', '--')}"


def get_model() -> SentenceTransformer:
    global _model
    if not embeddings_available():
        raise RuntimeError(_MISSING_EMBEDDINGS_MESSAGE)
    # Keep this import after the HF environment configuration above: importing
    # sentence-transformers eagerly initializes its Hugging Face dependencies.
    from sentence_transformers import SentenceTransformer

    if _model is None:
        if not _model_cache_dir().exists():
            logger.info(
                "loading embedding model %s (first load downloads ~44MB, then cached at ~/.cache/huggingface)",
                MODEL_NAME,
            )
        _model = SentenceTransformer(MODEL_NAME)
    return _model


def embed(text: str) -> list[float]:
    # P3 hardening: an empty or whitespace-only string embeds to a near-zero
    # vector that matches everything similarly. That silently poisons retrieval
    # and dedup downstream. Better to refuse at the boundary so the caller
    # finds the bug instead of getting nonsense neighbors.
    if text is None or not text.strip():
        raise ValueError("embed(): refusing to embed empty/whitespace text")
    return get_model().encode(text, normalize_embeddings=True).tolist()
