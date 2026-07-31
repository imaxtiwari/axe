"""Lightweight, dependency-light embedding models for signal/assumption filtering."""

from __future__ import annotations

import hashlib
import math
from abc import ABC, abstractmethod
from typing import Any


class EmbeddingModel(ABC):
    """Abstract embedding model."""

    @abstractmethod
    async def embed(self, text: str) -> list[float]: ...

    @abstractmethod
    def embedding_dim(self) -> int: ...


def _l2_normalize(vector: list[float]) -> list[float]:
    magnitude = math.sqrt(sum(v * v for v in vector)) or 1.0
    return [v / magnitude for v in vector]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two equal-length vectors."""
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a)) or 1.0
    norm_b = math.sqrt(sum(y * y for y in b)) or 1.0
    score = dot / (norm_a * norm_b)
    # Guard against tiny floating-point overshoot.
    return max(-1.0, min(1.0, score))


class HashEmbedding(EmbeddingModel):
    """Deterministic, dependency-free embedding via hashed n-gram averages.

    Not semantically rich, but reproducible and useful as an offline baseline or
    test fallback.
    """

    def __init__(self, dim: int = 384, ngram: int = 2) -> None:
        self.dim = dim
        self.ngram = ngram

    def embedding_dim(self) -> int:
        return self.dim

    async def embed(self, text: str) -> list[float]:
        text = (text or "").lower()
        chars = list(text)
        vector = [0.0] * self.dim
        count = 0
        for i in range(len(chars) - self.ngram + 1):
            gram = "".join(chars[i : i + self.ngram])
            digest = hashlib.sha256(gram.encode()).digest()
            for j in range(self.dim):
                # Spread the byte-derived signal across dimensions deterministically.
                byte = digest[j % len(digest)]
                vector[j] += (byte / 128.0) - 1.0
            count += 1
        if count == 0:
            return vector
        averaged = [v / count for v in vector]
        return _l2_normalize(averaged)


class MockEmbedding(EmbeddingModel):
    """Test embedding model returning a configurable vector for any input."""

    def __init__(self, vector: list[float] | None = None, dim: int = 384) -> None:
        self._vector = vector or ([1.0] + [0.0] * (dim - 1))
        self._dim = len(self._vector)

    def embedding_dim(self) -> int:
        return self._dim

    async def embed(self, text: str) -> list[float]:
        return list(self._vector)


class ThresholdMockEmbedding(EmbeddingModel):
    """Mock embedding whose similarity between successive calls is deterministic."""

    def __init__(self, similarity: float = 0.85, dim: int = 384) -> None:
        self._similarity = max(-1.0, min(1.0, similarity))
        self._dim = dim
        self._call_count = 0

    def embedding_dim(self) -> int:
        return self._dim

    async def embed(self, text: str) -> list[float]:
        # Return alternating vectors: first signal, then assumption, etc.
        # Pair-wise cosine similarity equals _similarity by construction.
        self._call_count += 1
        if self._call_count % 2 == 1:
            return _l2_normalize([1.0] + [0.0] * (self._dim - 1))
        a = self._similarity
        b = math.sqrt(max(0.0, 1.0 - a * a))
        return _l2_normalize([a, b] + [0.0] * (self._dim - 2))


def get_default_embedding_model() -> EmbeddingModel:
    """Return a deterministic baseline embedding model."""
    return HashEmbedding()


__all__ = [
    "EmbeddingModel",
    "HashEmbedding",
    "MockEmbedding",
    "ThresholdMockEmbedding",
    "cosine_similarity",
    "get_default_embedding_model",
]
