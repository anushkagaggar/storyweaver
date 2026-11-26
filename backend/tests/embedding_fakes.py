"""A fake `Embedder` for tests: deterministic vectors (the same text always gives the same 384 numbers, and
different texts give different ones), no model, no network. It records what it was asked to embed."""

import hashlib

import numpy as np

from storyweaver.storage.models import EMBEDDING_DIM


def fake_vector(text: str, dimension: int = EMBEDDING_DIM) -> list[float]:
    seed = int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big")
    raw = np.random.default_rng(seed).normal(size=dimension)
    return (raw / np.linalg.norm(raw)).tolist()


class FakeEmbedder:
    name = "fake-embedder"

    def __init__(self, dimension: int = EMBEDDING_DIM) -> None:
        self.dimension = dimension
        self.passage_calls: list[list[str]] = []
        self.query_calls: list[str] = []

    @property
    def passages_embedded(self) -> list[str]:
        return [text for call in self.passage_calls for text in call]

    async def embed_passages(self, texts: list[str]) -> list[list[float]]:
        self.passage_calls.append(list(texts))
        return [fake_vector(text, self.dimension) for text in texts]

    async def embed_query(self, text: str) -> list[float]:
        self.query_calls.append(text)
        return fake_vector(text, self.dimension)
