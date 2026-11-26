"""Opt-in live test of the real re-ranker (bge-reranker-base, int8) inside the real retrieval chain.

Run (needs the Docker Postgres up; the model files, about 280 MB with the tokenizer, download once):
  LIVE_LOCAL_EMBEDDER=1 EMBEDDER_DIR=D:/lenovo/models/bge-small RERANKER_DIR=D:/lenovo/models/bge-reranker \
  TEST_DATABASE_URL=postgresql+asyncpg://storyweaver:<password>@localhost:5432/storyweaver_test \
      uv run pytest -m live tests/live/test_reranker_live.py -s

The shelf is deliberately confusable: four tales with a wolf, three with a small animal helping a big
one, three about a race or a chase. A search by meaning alone finds "something wolfish"; the re-ranker has to
tell which wolf tale the child described. It prints how often each setup puts the right tale first (a
comparison to read, not a claim that re-ranking wins).
"""

import json
import os
import textwrap
from pathlib import Path

import pytest
from sqlalchemy import text

from storyweaver.adapters.bge_embedder import BgeSmallEmbedder
from storyweaver.adapters.bge_reranker import BgeReranker
from storyweaver.rag.loader import load_chunks
from storyweaver.rag.retriever import HybridRetriever, RetrievalQuery
from storyweaver.storage.db import create_engine, create_session_factory

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.getenv("LIVE_LOCAL_EMBEDDER"), reason="LIVE_LOCAL_EMBEDDER not set"),
]

_RAW = {
    "three_little_pigs": (
        "Three little pigs built houses of straw, sticks and bricks. A hungry wolf huffed and "
        "puffed and blew down the first two, but he could not blow down the house made of "
        "bricks."
    ),
    "red_riding_hood": (
        "A girl in a red hood took cakes to her grandmother. The wolf ran ahead, pretended to "
        "be the grandmother, and waited in her bed. A woodcutter heard the girl's cries and "
        "chased the wolf away."
    ),
    "boy_who_cried_wolf": (
        "A shepherd boy was bored, so he shouted that a wolf was coming and the villagers ran "
        "to help. He laughed at them. When a real wolf came, nobody believed his cries, and "
        "the sheep were lost."
    ),
    "wolf_and_seven_kids": (
        "A mother goat went out and told her seven kids never to open the door to the wolf. "
        "The wolf softened his voice with chalk and whitened his paw, and fooled six of them, "
        "but the youngest hid."
    ),
    "lion_and_mouse": (
        "A tiny mouse woke a sleeping lion, who spared her life. Later hunters trapped the "
        "lion in a net. The mouse gnawed through the ropes and set him free."
    ),
    "ant_and_dove": (
        "An ant fell into a stream and a dove dropped a leaf for her to climb on. Later a "
        "hunter aimed at the dove, and the ant bit his foot so the dove flew away in time."
    ),
    "elephant_and_mouse": (
        "A mouse who lived near the elephants' path was kind to a great elephant with a thorn "
        "in his foot, pulling it out with her tiny teeth, and he guarded her nest from then "
        "on."
    ),
    "tortoise_and_hare": (
        "A hare laughed at a tortoise for being so slow and challenged him to a race. The "
        "hare ran far ahead and lay down for a nap. The tortoise kept walking and crossed the "
        "line first."
    ),
    "gingerbread_man": (
        "An old woman baked a gingerbread man who jumped up and ran away. He raced past a cow "
        "and a horse, until a sly fox tricked him at the river and ate him."
    ),
    "fox_and_grapes": (
        "A hungry fox saw grapes high on a vine and jumped again and again but could not "
        "reach them. At last he walked away, saying that the grapes were probably sour "
        "anyway."
    ),
}
SHELF = {name: " ".join(textwrap.wrap(body)) for name, body in _RAW.items()}

QUERIES = {
    "a wolf who dresses up as a grandmother to trick a little girl": "red_riding_hood",
    "a wolf who cannot knock down a strong brick house": "three_little_pigs",
    "someone who lied about danger so no one believed the real warning": "boy_who_cried_wolf",
    "baby goats who are told not to open the door to a stranger": "wolf_and_seven_kids",
    "a tiny animal chews through a net to free a king of the beasts": "lion_and_mouse",
    "a bug is rescued from drowning and later returns the favour": "ant_and_dove",
    "the slow one wins because the fast one got cocky": "tortoise_and_hare",
    "a fox pretends he never wanted the thing he could not get": "fox_and_grapes",
}


async def test_the_real_reranker_helps_pick_the_right_tale(database_url: str, tmp_path: Path) -> None:
    path = tmp_path / "chunks.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(
                {
                    "source": "gutenberg",
                    "tale_id": f"gutenberg:{name}",
                    "position": 0,
                    "title": name.replace("_", " ").title(),
                    "age_band": "4-6",
                    "grade": 3.0,
                    "licence": "public-domain-us",
                    "text": body,
                }
            )
            for name, body in SHELF.items()
        ),
        encoding="utf-8",
    )
    embedder = BgeSmallEmbedder(
        directory=Path(os.getenv("EMBEDDER_DIR", str(Path.home() / ".cache" / "storyweaver" / "bge-small")))
    )
    reranker = BgeReranker(
        directory=Path(
            os.getenv("RERANKER_DIR", str(Path.home() / ".cache" / "storyweaver" / "bge-reranker"))
        )
    )
    engine = create_engine(database_url)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE tale_chunks"))
            await session.commit()
        await load_chunks(path, embedder=embedder, session_factory=factory)

        plain = HybridRetriever(session_factory=factory, embedder=embedder)
        reranked = HybridRetriever(session_factory=factory, embedder=embedder, reranker=reranker, fused_k=10)
        first = {"plain": 0, "reranked": 0}
        for question, expected in QUERIES.items():
            query = RetrievalQuery(idea=question)
            for label, retriever in (("plain", plain), ("reranked", reranked)):
                found = await retriever.search(query, age_band="4-6", limit=3)
                names = [chunk.tale_id.removeprefix("gutenberg:") for chunk in found]
                first[label] += names[0] == expected
                print(
                    f"{label:9} {'OK  ' if names[0] == expected else 'miss'} {question[:52]!r} -> {names[:2]}"
                )
        print(f"first place: {first} of {len(QUERIES)}")  # shown with -s
        # Not "reranked >= plain": whether re-ranking pays is what the gold-set evaluation decides. On this
        # shelf the search by meaning alone is already as good (8 of 8, against 7 for the re-ranker, which
        # misses one very abstract request); this only guards against the re-ranker being broken.
        assert first["reranked"] >= 6, first
    finally:
        async with factory() as session:
            await session.execute(text("TRUNCATE tale_chunks"))
            await session.commit()
        await engine.dispose()
