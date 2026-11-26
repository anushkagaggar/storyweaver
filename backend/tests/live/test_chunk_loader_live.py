"""Opt-in live test of the whole V4 loading chain: the real bge-small model, a real Postgres with pgvector,
and a meaning-based search over what was loaded (the fakes in the integration tests cannot show that).

Run (needs the Docker Postgres up; no account or key; uses the cached model, or downloads it once):
  LIVE_LOCAL_EMBEDDER=1 EMBEDDER_DIR=D:/lenovo/models/bge-small \
  TEST_DATABASE_URL=postgresql+asyncpg://storyweaver:<password>@localhost:5432/storyweaver_test \
      uv run pytest -m live tests/live/test_chunk_loader_live.py -s

The query shares no content words with the passage it must find, so only the embedding (not the full-text
column) can find it: that is what "story memory" is for.
"""

import json
import os
from pathlib import Path

import pytest
from sqlalchemy import select, text

from storyweaver.adapters.bge_embedder import BgeSmallEmbedder
from storyweaver.rag.loader import load_chunks
from storyweaver.storage.db import create_engine, create_session_factory
from storyweaver.storage.models import TaleChunkRow

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.getenv("LIVE_LOCAL_EMBEDDER"), reason="LIVE_LOCAL_EMBEDDER not set"),
]


TALES = {
    "gutenberg:tortoise": "The slow tortoise plodded on and on while the boastful hare napped by the road. "
    "When the hare woke, the tortoise was almost at the finish line, and he lost the race.",
    "gutenberg:lantern": "A little dragon was afraid of the dark, so a firefly lit the cave for him. "
    "From then on he slept with a tiny glowing friend beside his bed.",
    "gutenberg:shoes": "Each night the elves came out and stitched shoes for the poor cobbler. "
    "In the morning he found them finished and sold them for a good price.",
}


async def test_chunks_loaded_with_the_real_model_are_found_by_meaning(
    database_url: str, tmp_path: Path
) -> None:
    path = tmp_path / "chunks.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(
                {
                    "source": "gutenberg",
                    "tale_id": tale_id,
                    "position": 0,
                    "title": tale_id,
                    "age_band": "4-6",
                    "grade": 3.0,
                    "licence": "public-domain-us",
                    "text": body,
                }
            )
            for tale_id, body in TALES.items()
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

        report = await load_chunks(path, embedder=embedder, session_factory=factory)
        assert report.inserted == 3

        # "a scaredy-cat creature and an unlit room": no word in common with the dragon tale.
        query = await embedder.embed_query("a timid creature who was frightened of a room with no lights")
        async with factory() as session:
            rows = await session.execute(
                select(
                    TaleChunkRow.tale_id, TaleChunkRow.embedding.cosine_distance(query).label("distance")
                ).order_by("distance")
            )
            ranked = [(tale_id, round(float(distance), 3)) for tale_id, distance in rows]
        print(ranked)  # shown with -s
        assert ranked[0][0] == "gutenberg:lantern", ranked

        again = await load_chunks(path, embedder=embedder, session_factory=factory)
        assert (again.unchanged, again.embedded) == (3, 0)  # a rerun does no embedding at all
    finally:
        async with factory() as session:
            await session.execute(text("TRUNCATE tale_chunks"))
            await session.commit()
        await engine.dispose()
