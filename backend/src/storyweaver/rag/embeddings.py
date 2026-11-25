"""Provider-neutral text embeddings (plan §4.1, §9): the tale loader and the retriever talk to `Embedder`.

An embedding is a list of numbers that places a text in "meaning space": texts about similar things land
close together, which is what pgvector's nearest-neighbour search finds. Passages (stored) and queries
(searched with) are embedded differently, so they are separate methods.
"""

from collections.abc import Sequence
from typing import Protocol


class EmbeddingError(Exception):
    """Embedding failed in a way a retry won't fix."""


class EmbeddingUnavailableError(EmbeddingError):
    """The model could not be downloaded or loaded, or it returned something unexpected."""


class Embedder(Protocol):
    name: str  # recorded with the data it produced, e.g. "bge-small-en-v1.5"
    dimension: int  # must equal the `tale_chunks.embedding` column size (storage.models.EMBEDDING_DIM)

    async def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed texts to be stored and searched, in order. Vectors have length 1 (they are normalised)."""
        ...

    async def embed_query(self, text: str) -> list[float]:
        """Embed one search query so that it lands near the passages that answer it."""
        ...
