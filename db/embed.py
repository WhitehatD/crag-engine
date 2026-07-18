#!/usr/bin/env python
"""
crag-anchor Embedding Service -- fastembed + all-MiniLM-L6-v2

Model: sentence-transformers/all-MiniLM-L6-v2
  Dims: 384 (float32)
  Size: ~23MB ONNX model
  Output: normalized L2 vectors (dot product == cosine similarity)

Environment:
  CRAG_ANCHOR_MODEL_CACHE   Override model cache dir.
                      Default: <crag-anchor-repo>/model-cache/
                      (resolved from this file's location: db/embed.py ->
                       parent.parent = crag-anchor-repo root).
                      We NEVER fall through to fastembed's built-in default
                      because that resolves to %TEMP%\fastembed_cache, which is
                      a volatile directory that OS cleaners and temp-management
                      tools delete without warning, causing daemon startup
                      failures (root cause: 2026-05-23 NoSuchFile incident).

Usage:
  from embed import embed_text, embed_batch, cosine_sim, bytes_to_vec
"""

import os
from pathlib import Path
import numpy as np

EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_DIMS = 384
_model = None  # module-level singleton

# Permanent model cache directory — lives inside the crag-anchor repo, never in TEMP.
# db/embed.py -> .parent = db/ -> .parent = crag-anchor-repo root -> / model-cache
_DEFAULT_CACHE_DIR = str(Path(__file__).resolve().parent.parent / "model-cache")


def get_model():
    """Return cached fastembed TextEmbedding model. Loads on first call."""
    global _model
    if _model is not None:
        return _model
    try:
        from fastembed import TextEmbedding
    except ImportError:
        raise RuntimeError(
            "fastembed not installed. Run: pip install fastembed"
        )
    kwargs = {"model_name": EMBEDDING_MODEL}
    # Always set cache_dir explicitly — CRAG_ANCHOR_MODEL_CACHE env var takes
    # precedence; otherwise fall back to the repo-local default.
    # Never omit cache_dir (which would allow fastembed to use %TEMP%).
    cache_dir = os.environ.get("CRAG_ANCHOR_MODEL_CACHE") or _DEFAULT_CACHE_DIR
    kwargs["cache_dir"] = cache_dir
    _model = TextEmbedding(**kwargs)
    return _model


def embed_text(text: str) -> bytes:
    """Embed a single string. Returns float32 bytes (384*4=1536 bytes)."""
    model = get_model()
    vecs = list(model.embed([text]))
    return vecs[0].astype("float32").tobytes()


def embed_batch(texts: list) -> list:
    """Embed multiple texts. Returns list of float32 byte strings."""
    model = get_model()
    return [v.astype("float32").tobytes() for v in model.embed(texts)]


def bytes_to_vec(b: bytes) -> np.ndarray:
    """Deserialize stored embedding bytes to numpy array."""
    return np.frombuffer(b, dtype="float32")


def cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity. MiniLM outputs L2-normalized vecs so dot product == cosine."""
    norm_a, norm_b = np.linalg.norm(a), np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))
