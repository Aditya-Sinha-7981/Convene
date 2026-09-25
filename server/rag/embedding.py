"""Local embedding adapter shared by CON-08 ingestion and later CON-09 retrieval."""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Protocol, Sequence

import numpy as np

from ..config import EmbeddingModelConfig


class EmbeddingAdapter(Protocol):
    model_identifier: str
    runtime: str
    dimension: int

    def load(self) -> None: ...
    def count_tokens(self, text: str) -> int: ...
    def embed(self, texts: Sequence[str]) -> list[np.ndarray]: ...


@dataclass
class FakeEmbeddingAdapter:
    """Deterministic test adapter; it deliberately has no semantic retrieval behavior."""
    dimension: int = 3
    model_identifier: str = "fake-embedding"
    runtime: str = "sentence_transformers"

    def load(self) -> None:
        return None

    def count_tokens(self, text: str) -> int:
        return len(text.split())

    def embed(self, texts: Sequence[str]) -> list[np.ndarray]:
        vectors = []
        for text in texts:
            values = np.zeros(self.dimension, dtype=np.float32)
            for index, byte in enumerate(text.encode("utf-8")):
                values[index % self.dimension] += byte
            norm = np.linalg.norm(values)
            vectors.append(values / norm if norm else values)
        return vectors


class SentenceTransformerAdapter:
    """Cache-only CPU adapter. Nothing in this class may download a model at meeting time."""
    runtime = "sentence_transformers"

    def __init__(self, config: EmbeddingModelConfig):
        self.config = config
        self.model_identifier = config.model
        self.dimension = config.dimension
        self._model = None
        self.load_seconds: float | None = None
        # The indexer and question embedding (CON-09) share this adapter from different worker threads; a Hugging
        # Face fast tokenizer must not be used concurrently, so calls are serialized. Each call takes milliseconds.
        self._lock = threading.RLock()

    def load(self) -> None:
        with self._lock:
            self._load()

    def _load(self) -> None:
        if self._model is not None:
            return
        from sentence_transformers import SentenceTransformer
        began = time.monotonic()
        # SentenceTransformer delegates to huggingface_hub; these flags make a missing cache an actionable error.
        old = os.environ.get("HF_HUB_OFFLINE")
        os.environ["HF_HUB_OFFLINE"] = "1"
        try:
            self._model = SentenceTransformer(self.config.model, revision=self.config.revision,
                                              local_files_only=True, device="cpu")
        except Exception as exc:
            raise RuntimeError(f"embedding model {self.config.model}@{self.config.revision} is not provisioned locally") from exc
        finally:
            if old is None:
                os.environ.pop("HF_HUB_OFFLINE", None)
            else:
                os.environ["HF_HUB_OFFLINE"] = old
        dimension = self._model.get_sentence_embedding_dimension()
        if dimension != self.config.dimension:
            raise RuntimeError(f"embedding model dimension {dimension} differs from configured {self.config.dimension}")
        self.load_seconds = time.monotonic() - began

    def count_tokens(self, text: str) -> int:
        with self._lock:
            self._load()
            return len(self._model.tokenizer.encode(text, add_special_tokens=True))

    def embed(self, texts: Sequence[str]) -> list[np.ndarray]:
        with self._lock:
            self._load()
            vectors = self._model.encode(list(texts), normalize_embeddings=True, convert_to_numpy=True,
                                         show_progress_bar=False)
        return [np.asarray(vector, dtype=np.float32) for vector in vectors]


def build_embedding_adapter(config: EmbeddingModelConfig) -> EmbeddingAdapter:
    if config.runtime != "sentence_transformers":
        raise ValueError(f"unsupported embedding runtime {config.runtime!r}")
    return SentenceTransformerAdapter(config)
