"""Provider-neutral re-ranking (plan §4.1, §9 step 4): the retriever talks to `Reranker`.

Search by meaning compares a query vector with passage vectors that were made separately, so it is fast but
coarse. A re-ranker (a "cross-encoder") reads the query and one passage *together* and scores how well the
passage answers the query. That is slower, so it only re-orders the handful of candidates the search found.
"""

from collections.abc import Sequence
from typing import Protocol


class RerankError(Exception):
    """Re-ranking failed in a way a retry won't fix."""


class RerankUnavailableError(RerankError):
    """The model could not be downloaded or loaded, or it returned something unexpected."""


class Reranker(Protocol):
    name: str  # e.g. "bge-reranker-base"

    async def rerank(self, query: str, passages: Sequence[str]) -> list[float]:
        """One score per passage, in order; higher means a better match. Scores are only comparable
        between passages scored for the same query."""
        ...
