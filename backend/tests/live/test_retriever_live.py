"""Opt-in live test of retrieval end to end: the real bge-small model, a real Postgres with pgvector, the
loader and the hybrid retriever, over a small shelf of classic-tale passages (retold in our own words).

Run (needs the Docker Postgres up; no account or key; uses the cached model, or downloads it once):
  LIVE_LOCAL_EMBEDDER=1 EMBEDDER_DIR=D:/lenovo/models/bge-small \
  TEST_DATABASE_URL=postgresql+asyncpg://storyweaver:<password>@localhost:5432/storyweaver_test \
      uv run pytest -m live tests/live/test_retriever_live.py -s

The queries are written the way a child might ask, mostly sharing no words with the tale they should find.
It is a smoke test of the whole chain, not the evaluation: the 50-query gold set (recall@5, MRR) comes later.
"""

import json
import os
from pathlib import Path

import pytest
from sqlalchemy import text

from storyweaver.adapters.bge_embedder import BgeSmallEmbedder
from storyweaver.rag.loader import load_chunks
from storyweaver.rag.retriever import HybridRetriever, RetrievalQuery
from storyweaver.storage.db import create_engine, create_session_factory

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.getenv("LIVE_LOCAL_EMBEDDER"), reason="LIVE_LOCAL_EMBEDDER not set"),
]

SHELF = {
    "tortoise_and_hare": (
        "A hare laughed at a tortoise for being so slow and challenged him to a race. The "
        "hare ran far ahead and lay down for a nap. The tortoise kept walking, steady and "
        "patient, and crossed the line first."
    ),
    "ugly_duckling": (
        "Among the new ducklings was one big grey one that the others pecked and teased. "
        "Lonely and sad, he hid all winter. In spring he saw his reflection in the pond and "
        "found he had become a beautiful swan."
    ),
    "three_little_pigs": (
        "Three little pigs built houses of straw, of sticks and of bricks. A hungry wolf "
        "huffed and puffed and blew down the first two, but he could not blow down the house "
        "made of bricks."
    ),
    "elves_and_shoemaker": (
        "A poor shoemaker had leather for only one pair of shoes. In the morning they were "
        "made. Each night tiny elves worked while he slept, until he saw them and thanked "
        "them with little clothes."
    ),
    "thumbelina": (
        "A woman wished for a child and a girl no bigger than a thumb was born in a flower. "
        "She slept in a walnut shell, and a kind swallow finally carried her away to a land "
        "of flower people."
    ),
    "little_red_hen": (
        "A little red hen found some grains of wheat and asked who would help her plant them. "
        "The cat, the dog and the duck all said no. So she did every bit of the work herself, "
        "and ate the bread alone."
    ),
    "lion_and_mouse": (
        "A tiny mouse woke a sleeping lion, who spared her life. Later hunters trapped the "
        "lion in a net. The mouse gnawed through the ropes and set him free, proving that "
        "even the small can help the mighty."
    ),
    "boy_who_cried_wolf": (
        "A shepherd boy was bored, so he shouted that a wolf was coming and the villagers ran "
        "to help. He laughed at them. When a real wolf came, nobody believed his cries, and "
        "the sheep were lost."
    ),
    "gingerbread_man": (
        "An old woman baked a gingerbread man who jumped up and ran away, calling: run, run, "
        "as fast as you can, you can't catch me! He raced past a cow and a horse, until a sly "
        "fox tricked him at the river."
    ),
    "dragon_and_the_dark": (
        "A little dragon was afraid of the dark, so a firefly lit the cave for him. From then "
        "on he slept with a tiny glowing friend beside his bed."
    ),
}

QUERIES = {
    "a story about a slow animal who wins a race": "tortoise_and_hare",
    "a sad bird nobody wants to play with because he looks different": "ugly_duckling",
    "houses made of straw and sticks and bricks": "three_little_pigs",
    "magic helpers who work at night while the shoemaker sleeps": "elves_and_shoemaker",
    "a tiny girl who lives inside a flower": "thumbelina",
    "an animal who does all the work and nobody helps her": "little_red_hen",
    "a small mouse saves a big cat-like king of the jungle": "lion_and_mouse",
    "a boy who tells lies for fun and then nobody believes him": "boy_who_cried_wolf",
    "a cookie that runs away from everyone": "gingerbread_man",
    "a creature who is scared of the night and a glowing insect": "dragon_and_the_dark",
}


async def test_children_s_requests_find_the_tale_that_fits(database_url: str, tmp_path: Path) -> None:
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
    directory = Path(os.getenv("EMBEDDER_DIR", str(Path.home() / ".cache" / "storyweaver" / "bge-small")))
    embedder = BgeSmallEmbedder(directory=directory)
    engine = create_engine(database_url)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE tale_chunks"))
            await session.commit()
        assert (await load_chunks(path, embedder=embedder, session_factory=factory)).inserted == len(SHELF)

        retriever = HybridRetriever(session_factory=factory, embedder=embedder)
        top1 = 0
        for question, expected in QUERIES.items():
            found = await retriever.search(RetrievalQuery(idea=question), age_band="4-6", limit=3)
            names = [chunk.tale_id.removeprefix("gutenberg:") for chunk in found]
            print(f"{'OK  ' if names[0] == expected else 'miss'} {question!r} -> {names}")  # shown with -s
            assert expected in names, f"{question!r}: wanted {expected}, got {names}"
            top1 += names[0] == expected
        assert top1 >= 8, f"only {top1} of {len(QUERIES)} were the first result"
    finally:
        async with factory() as session:
            await session.execute(text("TRUNCATE tale_chunks"))
            await session.commit()
        await engine.dispose()
