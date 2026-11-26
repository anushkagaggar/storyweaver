"""Hybrid retrieval against a real Postgres with pgvector: the meaning search, the word search, fusion, the
age filter, one chunk per tale and the re-ranking slot. The fake embedder gives each text its own fixed
vector, so a query equal to a chunk's text is nearest to exactly that chunk."""

from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from embedding_fakes import FakeEmbedder, fake_vector
from storyweaver.rag.retriever import HybridRetriever, RetrievalQuery
from storyweaver.storage.db import create_engine, create_session_factory
from storyweaver.storage.models import TaleChunkRow

pytestmark = pytest.mark.integration

DRAGON = "A tiny dragon was afraid of the dark, so a firefly lit the cave for him."
HEDGEHOG = "The hedgehog splashed in a muddy puddle and her prickles dripped with rain."
SHIP = "The old captain steered his ship through the storm while the waves crashed over the deck."
ELVES = "Each night the elves stitched shoes for the poor cobbler, who woke to find them finished."


@pytest.fixture
async def factory(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(database_url)
    session_factory = create_session_factory(engine)
    async with session_factory() as session:
        await session.execute(text("TRUNCATE tale_chunks"))
        await session.commit()
    yield session_factory
    async with session_factory() as session:
        await session.execute(text("TRUNCATE tale_chunks"))
        await session.commit()
    await engine.dispose()


async def _add(
    factory: async_sessionmaker[AsyncSession], tale: str, position: int, body: str, band: str = "4-6"
) -> None:
    async with factory() as session:
        session.add(
            TaleChunkRow(
                source="gutenberg",
                tale_id=f"gutenberg:{tale}",
                position=position,
                title=tale.title(),
                age_band=band,
                licence="public-domain-us",
                text=body,
                embedding=fake_vector(body),
            )
        )
        await session.commit()


async def _four_tales(factory: async_sessionmaker[AsyncSession]) -> None:
    for tale, body in (("dragon", DRAGON), ("hedgehog", HEDGEHOG), ("ship", SHIP), ("elves", ELVES)):
        await _add(factory, tale, 0, body)


def _retriever(factory: Any, embedder: FakeEmbedder | None = None, **options: Any) -> HybridRetriever:
    return HybridRetriever(session_factory=factory, embedder=embedder or FakeEmbedder(), **options)


async def test_the_meaning_search_finds_the_nearest_chunk(factory: Any) -> None:
    await _four_tales(factory)

    found = await _retriever(factory).search(RetrievalQuery(idea=SHIP), age_band="4-6")

    assert found[0].tale_id == "gutenberg:ship"
    assert found[0].vector_rank == 1


async def test_the_word_search_finds_a_rare_word_the_meaning_search_misses(factory: Any) -> None:
    await _four_tales(factory)
    # One meaning-candidate only (a random neighbour): the hedgehog can only get in through its word.
    retriever = _retriever(factory, vector_k=1, final_k=4)

    found = await retriever.search(RetrievalQuery(idea="hedgehog"), age_band="4-6")

    hedgehog = next(chunk for chunk in found if chunk.tale_id == "gutenberg:hedgehog")
    assert hedgehog.text_rank == 1


async def test_a_chunk_found_both_ways_outranks_one_found_one_way(factory: Any) -> None:
    await _add(factory, "a", 0, "The hedgehog and the fox shared one lantern in the dark wood.")
    await _add(factory, "b", 0, "A hedgehog slept all winter long under the old oak tree.")
    await _four_tales(factory)

    found = await _retriever(factory, final_k=4).search(
        RetrievalQuery(idea="The hedgehog and the fox shared one lantern in the dark wood."), age_band="4-6"
    )

    assert found[0].tale_id == "gutenberg:a"
    assert found[0].vector_rank == 1 and found[0].text_rank == 1
    assert found[0].score > found[1].score


async def test_only_allowed_age_bands_are_returned(factory: Any) -> None:
    await _add(factory, "easy", 0, DRAGON, "4-6")
    await _add(factory, "middle", 0, DRAGON + " It lived in a cave.", "7-9")
    await _add(factory, "hard", 0, DRAGON + " It was a dragon of the old school.", "10+")
    retriever = _retriever(factory, final_k=5)
    query = RetrievalQuery(idea="a tiny dragon afraid of the dark with a firefly")

    for_young = {chunk.tale_id for chunk in await retriever.search(query, age_band="4-6")}
    for_older = {chunk.tale_id for chunk in await retriever.search(query, age_band="7-9")}
    for_oldest = {chunk.tale_id for chunk in await retriever.search(query, age_band="10+")}

    assert for_young == {"gutenberg:easy", "gutenberg:middle"}  # one band up is allowed, the hardest is not
    assert for_older == {"gutenberg:middle"}
    assert for_oldest == {"gutenberg:hard"}


async def test_a_tale_appears_once_unless_more_are_allowed(factory: Any) -> None:
    await _add(factory, "dragon", 0, DRAGON)
    await _add(factory, "dragon", 1, DRAGON + " The firefly stayed.")
    await _add(factory, "ship", 0, SHIP)
    query = RetrievalQuery(idea=DRAGON)

    once = await _retriever(factory, final_k=5).search(query, age_band="4-6")
    twice = await _retriever(factory, final_k=5, per_tale=2).search(query, age_band="4-6")

    assert [chunk.tale_id for chunk in once].count("gutenberg:dragon") == 1
    assert once[0].position == 0  # the better of the two
    assert [chunk.tale_id for chunk in twice].count("gutenberg:dragon") == 2


async def test_limit_caps_the_result_and_defaults_to_final_k(factory: Any) -> None:
    await _four_tales(factory)
    retriever = _retriever(factory, final_k=3)
    query = RetrievalQuery(idea=SHIP)

    assert len(await retriever.search(query, age_band="4-6")) == 3
    assert len(await retriever.search(query, age_band="4-6", limit=1)) == 1
    assert len(await retriever.search(query, age_band="4-6", limit=4)) == 4


async def test_an_empty_table_and_an_empty_query_give_nothing(factory: Any) -> None:
    embedder = FakeEmbedder()
    retriever = _retriever(factory, embedder)
    assert await retriever.search(RetrievalQuery(idea="a dragon"), age_band="4-6") == []
    assert await retriever.search(RetrievalQuery(idea="   "), age_band="4-6") == []
    assert embedder.query_calls == ["a dragon"]  # an empty query never reaches the model


async def test_a_request_with_only_generic_words_still_works_by_meaning(factory: Any) -> None:
    await _four_tales(factory)
    found = await _retriever(factory).search(RetrievalQuery(idea="tell me a little story"), age_band="4-6")
    assert len(found) == 3
    assert all(chunk.text_rank is None for chunk in found)  # no word search ran


async def test_hostile_query_text_is_harmless(factory: Any) -> None:
    await _four_tales(factory)
    nasty = "'); DROP TABLE tale_chunks; -- & ! ( ) <-> :* | dragon"

    found = await _retriever(factory).search(RetrievalQuery(idea=nasty), age_band="4-6")

    assert len(found) == 3
    async with factory() as session:
        count = await session.execute(text("SELECT count(*) FROM tale_chunks"))
        assert count.scalar_one() == 4


class ReversingReranker:
    """Prefers whatever the fused order liked least, so it is obvious when it has run."""

    name = "fake-reranker"

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []

    async def rerank(self, query: str, passages: Sequence[str]) -> list[float]:
        self.calls.append((query, list(passages)))
        return [float(index) for index in range(len(passages))]


async def test_the_reranker_reorders_the_candidates_and_its_scores_are_kept(factory: Any) -> None:
    await _four_tales(factory)
    reranker = ReversingReranker()
    retriever = _retriever(factory, reranker=reranker, fused_k=4, final_k=2)

    found = await retriever.search(RetrievalQuery(idea=SHIP), age_band="4-6")

    [(query, passages)] = reranker.calls
    assert query == SHIP and len(passages) == 4  # it saw the fused candidates, not just the final two
    assert passages[0] == SHIP  # ...in fused order (the exact match first)
    assert found[0].text == passages[-1]  # and the reversal won
    assert found[0].rerank_score == 3.0 and found[0].score == 3.0
    assert len(found) == 2


async def test_a_reranker_that_miscounts_is_an_error(factory: Any) -> None:
    class Broken:
        name = "broken"

        async def rerank(self, query: str, passages: Sequence[str]) -> list[float]:
            return [1.0]

    await _four_tales(factory)
    with pytest.raises(ValueError, match="number of scores"):
        await _retriever(factory, reranker=Broken()).search(RetrievalQuery(idea=SHIP), age_band="4-6")


async def test_a_single_candidate_is_not_reranked(factory: Any) -> None:
    await _add(factory, "dragon", 0, DRAGON)
    reranker = ReversingReranker()
    found = await _retriever(factory, reranker=reranker).search(RetrievalQuery(idea=DRAGON), age_band="4-6")
    assert len(found) == 1 and reranker.calls == []


async def test_bad_settings_are_refused(factory: Any) -> None:
    with pytest.raises(ValueError, match="at least 1"):
        _retriever(factory, final_k=0)
