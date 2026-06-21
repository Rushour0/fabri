import logging
import os
from pathlib import Path

# Mute HuggingFace / sentence-transformers chatter BEFORE the import. The
# `Loading weights` tqdm bar and the "unauthenticated requests to the HF Hub"
# warning otherwise leak straight to stderr on every `fabri run`.
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

from sentence_transformers import SentenceTransformer  # noqa: E402

logging.getLogger("sentence_transformers").setLevel(logging.WARNING)
logging.getLogger("transformers").setLevel(logging.ERROR)
logging.getLogger("huggingface_hub").setLevel(logging.ERROR)

logger = logging.getLogger("fabri")

MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_DIM = 384

_model: SentenceTransformer | None = None


def _model_cache_dir() -> Path:
    hf_home = os.environ.get("HF_HOME") or os.environ.get("HUGGINGFACE_HUB_CACHE")
    base = Path(hf_home) if hf_home else Path.home() / ".cache" / "huggingface"
    return base / "hub" / f"models--{MODEL_NAME.replace('/', '--')}"


def get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        if not _model_cache_dir().exists():
            logger.info(
                "loading embedding model %s (first load downloads ~44MB, then cached at ~/.cache/huggingface)",
                MODEL_NAME,
            )
        _model = SentenceTransformer(MODEL_NAME)
    return _model


def embed(text: str) -> list[float]:
    return get_model().encode(text, normalize_embeddings=True).tolist()
