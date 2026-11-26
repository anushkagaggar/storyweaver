"""Hybrid retrieval over `tale_chunks` (V4, plan §9 steps 3 and 4): find the tales that can inspire a story.

Two searches run over the same table and their results are merged:

- **by meaning** (pgvector): the query is embedded and the 20 nearest chunks by cosine distance are taken.
  It finds "a timid creature afraid of a dark room" for a tale about a dragon and a firefly, no word shared.
- **by words** (Postgres full-text): chunks containing any of the query's words, best match first. It finds
  what meaning-search blurs, like a name ("Thumbelina") or a rare noun ("hedgehog").

Each search gives a ranked list. **Reciprocal-rank fusion** (RRF) merges them without having to compare
their scores (cosine distances and ts_rank values are not on the same scale): a chunk scores 1/(k+rank) in
each list it appears in, and the scores add up. A chunk found by both searches beats one found by one.

Then: keep the best chunk of each tale (three chunks of the same story are not three ideas), optionally
re-rank the best few with a cross-encoder (`Reranker`; none is wired in yet, so the fused order stands), and
return the top few. Retrieval is an enhancement: callers should treat a failure as "no inspiration", never
as "no story".
"""

import logging
import re
import uuid
from collections.abc import Hashable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from storyweaver.rag.embeddings import Embedder
from storyweaver.storage.models import TaleChunkRow

logger = logging.getLogger(__name__)

VECTOR_K = 20  # candidates from the meaning search
TEXT_K = 20  # candidates from the word search
RRF_K = 60  # the constant from the original RRF paper; it softens the lead of the top ranks
FUSED_K = 10  # candidates kept for the (optional) re-ranker
FINAL_K = 3  # chunks handed to the story planner
EF_SEARCH = 100  # how widely the HNSW index looks before the age filter is applied (pgvector's default is 40)

# Which corpus age bands may inspire a story for each requested band. The corpus bands come from the
# Flesch-Kincaid grade of the passage ("4-6": grade <= 4, "7-9": <= 7, "10+": harder). A 4-6 story may draw on
# one band up, because the retrieved text is only inspiration (it is summarised, never copied), but not on
# dense or archaic prose. A band missing here gets only itself.
ALLOWED_BANDS: dict[str, tuple[str, ...]] = {"4-6": ("4-6", "7-9")}

# Words that say "this is a story request" and tell the word search nothing about which story.
GENERIC_WORDS = frozenset(
    "story stories tale tales tell write make please little once upon time day child children kid kids "
    "want like about with that this there their".split()
)
_WORD = re.compile(r"[a-z0-9]{3,}")


def bands_for(age_band: str) -> tuple[str, ...]:
    return ALLOWED_BANDS.get(age_band, (age_band,))


@dataclass(frozen=True)
class RetrievalQuery:
    """What to look for: the child's idea, plus whatever the query-understanding step extracted from it."""

    idea: str
    themes: Sequence[str] = ()
    characters: Sequence[str] = ()
    moral: str = ""
    setting: str = ""

    def search_text(self) -> str:
        parts = [self.idea.strip()]
        for label, value in (
            ("Themes", ", ".join(self.themes)),
            ("Characters", ", ".join(self.characters)),
            ("Moral", self.moral),
            ("Setting", self.setting),
        ):
            value = value.strip().rstrip(".")
            if value:
                parts.append(f"{label}: {value}.")
        return " ".join(part for part in parts if part)


@dataclass(frozen=True)
class RetrievedChunk:
    id: uuid.UUID
    tale_id: str
    position: int
    title: str
    theme: str | None
    moral: str | None
    age_band: str
    text: str
    score: float  # the RRF score, or the re-ranker's when one ran
    vector_rank: int | None = None  # 1-based place in the meaning search, if it was found there
    text_rank: int | None = None  # ... and in the word search
    rerank_score: float | None = field(default=None, compare=False)


class Reranker(Protocol):
    """A cross-encoder: reads the query and each passage together and scores how well they match."""

    name: str

    async def rerank(self, query: str, passages: Sequence[str]) -> list[float]:
        """One score per passage, in order; higher is better."""
        ...


def reciprocal_rank_fusion[K: Hashable](
    rankings: Sequence[Sequence[K]], k: int = RRF_K
) -> list[tuple[K, float]]:
    """Merge ranked lists of keys. Returns (key, score), best first.

    Ties (equal scores) keep the order of first appearance, so the result is deterministic.
    """
    scores: dict[K, float] = {}
    for ranking in rankings:
        for rank, key in enumerate(ranking, start=1):
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
    # dicts keep insertion order and sorted() is stable, so ties stay in first-seen order
    return sorted(scores.items(), key=lambda item: item[1], reverse=True)


def build_tsquery(search_text: str) -> str | None:
    """'a shy hedgehog finds a lantern' -> 'shy | hedgehog | finds | lantern' (OR, not AND), or None.

    Only plain letters and digits survive, so the result can never carry tsquery syntax or quotes. Postgres's
    English configuration removes ordinary stop words ("a", "the") on its own; GENERIC_WORDS removes the
    words every story request contains. ts_rank ignores how common a word is, so the long tail of common
    words would otherwise drown the rare ones that matter.
    """
    seen: dict[str, None] = {}
    for word in _WORD.findall(search_text.lower()):
        if word not in GENERIC_WORDS:
            seen.setdefault(word)
    return " | ".join(seen) if seen else None


def one_per_tale(chunks: Iterable[RetrievedChunk], per_tale: int) -> list[RetrievedChunk]:
    """Keep at most `per_tale` chunks of each tale, the best-ranked ones (input order is rank order)."""
    taken: dict[str, int] = {}
    kept: list[RetrievedChunk] = []
    for chunk in chunks:
        if taken.get(chunk.tale_id, 0) < per_tale:
            taken[chunk.tale_id] = taken.get(chunk.tale_id, 0) + 1
            kept.append(chunk)
    return kept


_COLUMNS = (
    TaleChunkRow.id,
    TaleChunkRow.tale_id,
    TaleChunkRow.position,
    TaleChunkRow.title,
    TaleChunkRow.theme,
    TaleChunkRow.moral,
    TaleChunkRow.age_band,
    TaleChunkRow.text,
)


class HybridRetriever:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        embedder: Embedder,
        reranker: Reranker | None = None,
        vector_k: int = VECTOR_K,
        text_k: int = TEXT_K,
        fused_k: int = FUSED_K,
        final_k: int = FINAL_K,
        rrf_k: int = RRF_K,
        per_tale: int = 1,
        ef_search: int = EF_SEARCH,
    ) -> None:
        if min(vector_k, text_k, fused_k, final_k, per_tale) < 1:
            raise ValueError("candidate counts and per_tale must be at least 1")
        self._session_factory = session_factory
        self._embedder = embedder
        self._reranker = reranker
        self._vector_k = vector_k
        self._text_k = text_k
        self._fused_k = max(fused_k, final_k)
        self._final_k = final_k
        self._rrf_k = rrf_k
        self._per_tale = per_tale
        self._ef_search = ef_search

    async def search(
        self, query: RetrievalQuery, *, age_band: str, limit: int | None = None
    ) -> list[RetrievedChunk]:
        """The best chunks for `query`, for a story at `age_band`, best first (at most `limit`)."""
        wanted = limit or self._final_k
        search_text = query.search_text()
        if not search_text:
            return []
        bands = list(bands_for(age_band))
        vector = await self._embedder.embed_query(search_text)
        tsquery = build_tsquery(search_text)

        async with self._session_factory() as session, session.begin():
            # LOCAL: lasts for this transaction only.
            await session.execute(
                text("SELECT set_config('hnsw.ef_search', :value, true)"), {"value": str(self._ef_search)}
            )
            by_meaning = await session.execute(
                select(*_COLUMNS)
                .where(TaleChunkRow.age_band.in_(bands))
                .order_by(TaleChunkRow.embedding.cosine_distance(vector), TaleChunkRow.id)
                .limit(self._vector_k)
            )
            meaning_rows = list(by_meaning)
            word_rows: list[Any] = []
            if tsquery:
                tsq = func.to_tsquery("english", tsquery)
                by_words = await session.execute(
                    select(*_COLUMNS)
                    .where(TaleChunkRow.age_band.in_(bands), TaleChunkRow.tsv.op("@@")(tsq))
                    .order_by(func.ts_rank(TaleChunkRow.tsv, tsq).desc(), TaleChunkRow.id)
                    .limit(self._text_k)
                )
                word_rows = list(by_words)

        rows = {row.id: row for row in (*meaning_rows, *word_rows)}
        vector_rank = {row.id: rank for rank, row in enumerate(meaning_rows, start=1)}
        text_rank = {row.id: rank for rank, row in enumerate(word_rows, start=1)}
        fused = reciprocal_rank_fusion(
            [[row.id for row in meaning_rows], [row.id for row in word_rows]], k=self._rrf_k
        )
        chunks = [
            RetrievedChunk(
                id=chunk_id,
                tale_id=rows[chunk_id].tale_id,
                position=rows[chunk_id].position,
                title=rows[chunk_id].title,
                theme=rows[chunk_id].theme,
                moral=rows[chunk_id].moral,
                age_band=rows[chunk_id].age_band,
                text=rows[chunk_id].text,
                score=score,
                vector_rank=vector_rank.get(chunk_id),
                text_rank=text_rank.get(chunk_id),
            )
            for chunk_id, score in fused
        ]
        candidates = one_per_tale(chunks, self._per_tale)[: max(self._fused_k, wanted)]
        if self._reranker is not None and len(candidates) > 1:
            candidates = await self._rerank(self._reranker, search_text, candidates)
        return candidates[:wanted]

    @staticmethod
    async def _rerank(
        reranker: Reranker, search_text: str, candidates: list[RetrievedChunk]
    ) -> list[RetrievedChunk]:
        scores = await reranker.rerank(search_text, [chunk.text for chunk in candidates])
        if len(scores) != len(candidates):
            raise ValueError("the reranker returned a different number of scores than passages")
        ranked = sorted(zip(candidates, scores, strict=True), key=lambda pair: pair[1], reverse=True)
        return [replace(chunk, score=float(score), rerank_score=float(score)) for chunk, score in ranked]
