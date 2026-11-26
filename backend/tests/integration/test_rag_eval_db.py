"""evaluation/rag_eval.py against a real Postgres: reading the corpus, running requests through the real
retriever, and scoring them. The fake embedder gives each text its own fixed vector, so a request equal to a
passage's text is nearest to exactly that passage."""

import importlib.util
import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from embedding_fakes import FakeEmbedder, fake_vector
from storyweaver.rag.retriever import HybridRetriever
from storyweaver.storage.db import create_engine, create_session_factory
from storyweaver.storage.models import TaleChunkRow

pytestmark = pytest.mark.integration

_PATH = Path(__file__).resolve().parents[3] / "evaluation" / "rag_eval.py"
_spec = importlib.util.spec_from_file_location("rag_eval_db", _PATH)
assert _spec is not None and _spec.loader is not None
ev: Any = importlib.util.module_from_spec(_spec)
sys.modules["rag_eval_db"] = ev
_spec.loader.exec_module(ev)

DRAGON = "A tiny dragon was afraid of the dark, so a firefly lit the cave for him."
HEDGEHOG = "The hedgehog splashed in a muddy puddle and her prickles dripped with rain."
SHIP = "The old captain steered his ship through the storm while the waves crashed over the deck."
ADMIRAL = "The grave admiral addressed the assembled fleet from the quarterdeck at dawn."


@pytest.fixture
async def factory(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(database_url)
    session_factory = create_session_factory(engine)
    async with session_factory() as session:
        await session.execute(text("TRUNCATE tale_chunks"))
        for tale, body, band in (
            ("dragon", DRAGON, "4-6"),
            ("hedgehog", HEDGEHOG, "4-6"),
            ("ship", SHIP, "7-9"),
            ("navy", ADMIRAL, "10+"),
        ):
            session.add(
                TaleChunkRow(
                    source="gutenberg",
                    tale_id=f"gutenberg:{tale}",
                    position=0,
                    title=tale.title(),
                    age_band=band,
                    licence="public-domain-us",
                    text=body,
                    embedding=fake_vector(body),
                )
            )
        await session.commit()
    yield session_factory
    async with session_factory() as session:
        await session.execute(text("TRUNCATE tale_chunks"))
        await session.commit()
    await engine.dispose()


def _gold(*requests: tuple[str, str, list[str]]) -> list[Any]:
    return ev.parse_gold(
        [
            json.dumps({"id": qid, "kind": "named", "query": query, "all_of": patterns})
            for qid, query, patterns in requests
        ]
    )


async def test_the_corpus_is_read_with_ids_bands_and_text(factory: Any) -> None:
    corpus = await ev.read_corpus(factory)

    assert sorted(chunk.tale_id for chunk in corpus) == [
        "gutenberg:dragon",
        "gutenberg:hedgehog",
        "gutenberg:navy",
        "gutenberg:ship",
    ]
    ship = next(chunk for chunk in corpus if chunk.tale_id == "gutenberg:ship")
    assert ship.age_band == "7-9" and ship.text == SHIP and ship.title == "Ship"


async def test_requests_are_run_through_the_real_retriever_and_scored(factory: Any) -> None:
    gold = _gold(
        ("found", SHIP, ["captain"]),  # a 7-9 passage: allowed for a 4-6 story
        ("missed", DRAGON, ["hedgehog"]),  # the nearest passage is the dragon, not the hedgehog
        ("absent", "a tale nobody wrote", ["xyzzy"]),
        ("too_hard", ADMIRAL, ["admiral"]),  # only a 10+ passage matches
    )
    corpus = await ev.read_corpus(factory)
    judgements = ev.judge(gold, corpus, "4-6")
    retriever = HybridRetriever(session_factory=factory, embedder=FakeEmbedder())

    result = await ev.evaluate("plain", retriever, judgements, age_band="4-6", k=1)

    assert [j.status for j in judgements] == ["judged", "judged", "not_in_corpus", "filtered_by_age_band"]
    assert result.ranks == {"found": 1, "missed": None}  # the other two were never run
    assert result.overall.judged == 2 and result.overall.recall == 0.5
    assert result.top["found"][0].startswith("Ship: The old captain")
    # With room for five the hedgehog turns up behind the dragon (the fake vectors decide whether 2nd or 3rd).
    wider = await ev.evaluate("plain", retriever, judgements, age_band="4-6", k=5)
    assert wider.ranks["missed"] in (2, 3)


async def test_the_report_covers_the_run_end_to_end(factory: Any, tmp_path: Path) -> None:
    gold = _gold(("found", SHIP, ["captain"]), ("absent", "a tale nobody wrote", ["xyzzy"]))
    corpus = await ev.read_corpus(factory)
    judgements = ev.judge(gold, corpus, "4-6")
    result = await ev.evaluate(
        "plain",
        HybridRetriever(session_factory=factory, embedder=FakeEmbedder()),
        judgements,
        age_band="4-6",
        k=5,
    )

    summary = ev.render_summary(judgements, [result], corpus=corpus, age_band="4-6", k=5)
    ev.write_results(judgements, [result], summary, jsonl=tmp_path / "r.jsonl", md=tmp_path / "r.md")

    assert "4 passages from 4 books" in summary
    assert "1 not in the corpus" in summary
    rows = [json.loads(line) for line in (tmp_path / "r.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {row["id"]: row["status"] for row in rows} == {"found": "judged", "absent": "not_in_corpus"}
    assert rows[0]["rank"] == {"plain": 1}


async def test_a_prepared_query_is_searched_instead_of_the_request_as_written(factory: Any) -> None:
    from storyweaver.rag.retriever import RetrievalQuery

    gold = _gold(("missed", DRAGON, ["hedgehog"]))  # asked as written, the dragon passage is nearest
    corpus = await ev.read_corpus(factory)
    judgements = ev.judge(gold, corpus, "4-6")
    retriever = HybridRetriever(session_factory=factory, embedder=FakeEmbedder())

    as_written = await ev.evaluate("plain", retriever, judgements, age_band="4-6", k=1)
    prepared = await ev.evaluate(
        "enriched",
        retriever,
        judgements,
        age_band="4-6",
        k=1,
        queries={"missed": RetrievalQuery(idea=HEDGEHOG)},  # as if understanding had pointed at the hedgehog
    )

    assert as_written.ranks == {"missed": None}
    assert prepared.ranks == {"missed": 1}
