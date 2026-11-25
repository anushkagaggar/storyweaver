"""Opt-in live test of the real embedding model (plan §12: one live test per model).

Run (downloads ~133 MB once, with SHA-256 checks; no account or key needed):
  LIVE_LOCAL_EMBEDDER=1 EMBEDDER_DIR=D:/lenovo/models/bge-small \
      uv run pytest -m live tests/live/test_embedder_live.py -s

It proves what the unit tests (which use fakes) cannot: the pinned files are the right ones, the pooling and
the query instruction are the model's recipe, and "similar meaning" really means "nearby vector".
"""

import os
from pathlib import Path

import numpy as np
import pytest

from storyweaver.adapters.bge_embedder import DIMENSION, BgeSmallEmbedder

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.getenv("LIVE_LOCAL_EMBEDDER"), reason="LIVE_LOCAL_EMBEDDER not set"),
]

PASSAGES = {
    "dragon": "A little green dragon was afraid of the dark, so a firefly lit the cave for him.",
    "hedgehog": "The hedgehog splashed in a muddy puddle and her prickles dripped with rain.",
    "ship": "The old captain steered his ship through the storm and the waves crashed over the deck.",
}


def _embedder() -> BgeSmallEmbedder:
    directory = Path(os.getenv("EMBEDDER_DIR", str(Path.home() / ".cache" / "storyweaver" / "bge-small")))
    return BgeSmallEmbedder(directory=directory)


async def test_the_real_model_puts_similar_meanings_close_together() -> None:
    embedder = _embedder()

    vectors = dict(zip(PASSAGES, await embedder.embed_passages(list(PASSAGES.values())), strict=True))
    query = await embedder.embed_query("a story about a scared dragon and the dark")

    assert all(len(vector) == DIMENSION for vector in vectors.values())
    assert all(np.linalg.norm(vector) == pytest.approx(1.0, abs=1e-4) for vector in vectors.values())
    similarity = {name: float(np.dot(query, vector)) for name, vector in vectors.items()}
    assert max(similarity, key=similarity.__getitem__) == "dragon", similarity
    assert similarity["dragon"] > similarity["hedgehog"] + 0.05
    print(similarity)  # shown with -s
