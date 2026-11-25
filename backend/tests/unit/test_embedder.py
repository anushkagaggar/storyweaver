"""The bge-small embedder (V4): pooling maths, the query instruction, batching, and the pinned download.

The tokenizer and the onnxruntime session are replaced by fakes, so these tests need no model files; the one
live test (tests/live/test_embedder_live.py) runs the real model.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pytest
import respx

from storyweaver.adapters.bge_embedder import (
    DIMENSION,
    QUERY_INSTRUCTION,
    BgeSmallEmbedder,
    PinnedFile,
    cls_pool_and_normalise,
    ensure_file,
)
from storyweaver.rag.embeddings import EmbeddingUnavailableError
from storyweaver.storage.models import EMBEDDING_DIM

pytestmark = pytest.mark.unit


@dataclass
class _Encoding:
    ids: list[int]
    attention_mask: list[int]


class _FakeTokenizer:
    """Gives each text two tokens and remembers what it was asked to encode."""

    def __init__(self) -> None:
        self.seen: list[list[str]] = []

    def encode_batch(self, texts: list[str]) -> list[_Encoding]:
        self.seen.append(list(texts))
        return [_Encoding(ids=[101, 102], attention_mask=[1, 1]) for _ in texts]


@dataclass
class _Input:
    name: str


class _FakeSession:
    """Stands in for onnxruntime: the first token's vector is 3.0 at a position taken from the batch row."""

    def __init__(self, width: int = DIMENSION) -> None:
        self.width = width
        self.calls = 0
        self.fed: dict[str, Any] = {}

    def get_inputs(self) -> list[_Input]:
        return [_Input("input_ids"), _Input("attention_mask"), _Input("token_type_ids")]

    def run(self, _outputs: None, inputs: dict[str, Any]) -> list[Any]:
        self.calls += 1
        self.fed = inputs
        batch = inputs["input_ids"].shape[0]
        hidden = np.zeros((batch, 2, self.width), dtype=np.float32)
        for row in range(batch):
            hidden[row, 0, (self.calls * 10 + row) % self.width] = 3.0  # the first token decides the vector
            hidden[row, 1, 0] = 99.0  # other tokens must not matter
        return [hidden]


def _embedder(
    batch_size: int = 32, width: int = DIMENSION
) -> tuple[BgeSmallEmbedder, _FakeTokenizer, _FakeSession]:
    tokenizer, session = _FakeTokenizer(), _FakeSession(width)
    return (
        BgeSmallEmbedder(
            directory=Path("unused"), batch_size=batch_size, loader=lambda: (tokenizer, session)
        ),
        tokenizer,
        session,
    )


def test_the_column_size_and_the_model_size_agree() -> None:
    assert DIMENSION == EMBEDDING_DIM


def test_pooling_takes_the_first_token_and_scales_it_to_length_one() -> None:
    hidden = np.zeros((2, 3, 4), dtype=np.float32)
    hidden[0, 0] = [3.0, 4.0, 0.0, 0.0]
    hidden[0, 1] = [100.0, 100.0, 100.0, 100.0]  # ignored
    hidden[1, 0] = [0.0, 0.0, 0.0, 2.0]

    pooled = cls_pool_and_normalise(hidden)

    assert pooled.shape == (2, 4)
    assert pooled[0].tolist() == pytest.approx([0.6, 0.8, 0.0, 0.0])
    assert pooled[1].tolist() == pytest.approx([0.0, 0.0, 0.0, 1.0])


def test_pooling_a_zero_vector_gives_zeros_not_nan() -> None:
    pooled = cls_pool_and_normalise(np.zeros((1, 2, 4), dtype=np.float32))

    assert not np.isnan(pooled).any()
    assert pooled.tolist() == [[0.0, 0.0, 0.0, 0.0]]


async def test_passages_come_back_in_order_with_the_right_size_and_length() -> None:
    embedder, _tokenizer, _session = _embedder()

    vectors = await embedder.embed_passages(["one", "two", "three"])

    assert len(vectors) == 3
    assert all(len(vector) == DIMENSION for vector in vectors)
    assert all(np.linalg.norm(vector) == pytest.approx(1.0) for vector in vectors)
    assert len({vector.index(1.0) for vector in vectors}) == 3  # three different rows, kept in order


async def test_a_query_gets_the_instruction_and_a_passage_does_not() -> None:
    embedder, tokenizer, _session = _embedder()

    await embedder.embed_passages(["a dragon who was afraid of the dark"])
    await embedder.embed_query("a story about a scared dragon")

    assert tokenizer.seen[0] == ["a dragon who was afraid of the dark"]
    assert tokenizer.seen[1] == [QUERY_INSTRUCTION + "a story about a scared dragon"]


async def test_long_lists_run_in_batches_and_keep_their_order() -> None:
    embedder, tokenizer, session = _embedder(batch_size=2)

    vectors = await embedder.embed_passages(["a", "b", "c", "d", "e"])

    assert [len(batch) for batch in tokenizer.seen] == [2, 2, 1]
    assert session.calls == 3
    assert len(vectors) == 5


async def test_the_model_gets_a_mask_and_token_types_alongside_the_ids() -> None:
    embedder, _tokenizer, session = _embedder()

    await embedder.embed_passages(["hello"])

    assert set(session.fed) == {"input_ids", "attention_mask", "token_type_ids"}
    assert session.fed["input_ids"].dtype == np.int64
    assert not session.fed["token_type_ids"].any()


async def test_nothing_to_embed_means_the_model_is_never_loaded() -> None:
    def loader() -> tuple[Any, Any]:
        raise AssertionError("the model must not load for an empty list")

    embedder = BgeSmallEmbedder(directory=Path("unused"), loader=loader)

    assert await embedder.embed_passages([]) == []


async def test_a_model_that_will_not_load_is_reported_as_unavailable() -> None:
    def loader() -> tuple[Any, Any]:
        raise OSError("disk full")

    embedder = BgeSmallEmbedder(directory=Path("unused"), loader=loader)

    with pytest.raises(EmbeddingUnavailableError, match="OSError"):
        await embedder.embed_query("anything")


async def test_an_output_of_the_wrong_width_is_refused_rather_than_stored() -> None:
    embedder, _tokenizer, _session = _embedder(width=768)  # a different model's size

    with pytest.raises(EmbeddingUnavailableError, match="expected"):
        await embedder.embed_passages(["text"])


def test_a_batch_size_below_one_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="batch_size"):
        BgeSmallEmbedder(directory=Path("unused"), batch_size=0)


def _pinned(content: bytes) -> PinnedFile:
    return PinnedFile("onnx/model.onnx", hashlib.sha256(content).hexdigest())


@respx.mock
def test_a_download_is_kept_only_when_its_digest_matches(tmp_path: Path) -> None:
    content = b"pretend model weights"
    route = respx.get("https://models.test/onnx/model.onnx").respond(content=content)

    path = ensure_file(tmp_path, _pinned(content), base_url="https://models.test")
    again = ensure_file(tmp_path, _pinned(content), base_url="https://models.test")

    assert path == again == tmp_path / "model.onnx"
    assert path.read_bytes() == content
    assert route.call_count == 1  # the second call found a verified copy and downloaded nothing


@respx.mock
def test_a_download_with_the_wrong_digest_is_deleted(tmp_path: Path) -> None:
    respx.get("https://models.test/onnx/model.onnx").respond(content=b"tampered")

    with pytest.raises(EmbeddingUnavailableError, match="SHA-256"):
        ensure_file(tmp_path, _pinned(b"the real thing"), base_url="https://models.test")

    assert list(tmp_path.iterdir()) == []  # neither the file nor the .part leftovers


@respx.mock
def test_a_server_error_is_not_kept_as_a_model(tmp_path: Path) -> None:
    respx.get("https://models.test/onnx/model.onnx").respond(status_code=503)

    with pytest.raises(httpx.HTTPStatusError):
        ensure_file(tmp_path, _pinned(b"x"), base_url="https://models.test")

    assert not (tmp_path / "model.onnx").exists()
