"""The bge-reranker adapter (V4): pair encoding, batching, score shapes, lazy loading and failures.

The tokenizer and the onnxruntime session are replaced by fakes, so these tests need no model files; the live
test (tests/live/test_reranker_live.py) runs the real model. The pinned-download logic is shared with the
embedder and is tested in test_embedder.py.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from storyweaver.adapters import bge_reranker
from storyweaver.adapters.bge_reranker import BgeReranker
from storyweaver.rag.reranking import RerankUnavailableError

pytestmark = pytest.mark.unit


@dataclass
class _Encoding:
    ids: list[int]
    attention_mask: list[int]


class _FakeTokenizer:
    """Remembers the (query, passage) pairs it was asked to encode; the second id is the passage's length."""

    def __init__(self) -> None:
        self.seen: list[list[tuple[str, str]]] = []

    def encode_batch(self, pairs: list[tuple[str, str]]) -> list[_Encoding]:
        self.seen.append(list(pairs))
        return [_Encoding(ids=[0, len(passage), 2], attention_mask=[1, 1, 1]) for _, passage in pairs]


@dataclass
class _Input:
    name: str


class _FakeSession:
    """Scores each pair by its passage length (the second id), so longer passages score higher."""

    def __init__(self, *, with_token_types: bool = False, shape: str = "column") -> None:
        self.with_token_types = with_token_types
        self.shape = shape
        self.calls = 0
        self.fed: dict[str, Any] = {}

    def get_inputs(self) -> list[_Input]:
        names = ["input_ids", "attention_mask"] + (["token_type_ids"] if self.with_token_types else [])
        return [_Input(name) for name in names]

    def run(self, _outputs: None, inputs: dict[str, Any]) -> list[Any]:
        self.calls += 1
        self.fed = inputs
        scores = inputs["input_ids"][:, 1].astype(np.float32)
        if self.shape == "column":
            return [scores.reshape(-1, 1)]  # what the real model returns: one column, (batch, 1)
        if self.shape == "flat":
            return [scores]
        return [np.zeros((len(scores), 2), dtype=np.float32)]  # wrong: two logits per pair


def _reranker(
    batch_size: int = 16, **session_options: Any
) -> tuple[BgeReranker, _FakeTokenizer, _FakeSession]:
    tokenizer, session = _FakeTokenizer(), _FakeSession(**session_options)
    return (
        BgeReranker(directory=Path("unused"), batch_size=batch_size, loader=lambda: (tokenizer, session)),
        tokenizer,
        session,
    )


async def test_each_passage_is_scored_together_with_the_query() -> None:
    reranker, tokenizer, _ = _reranker()

    scores = await reranker.rerank("q", ["a", "bbb", "cc"])

    assert tokenizer.seen == [[("q", "a"), ("q", "bbb"), ("q", "cc")]]  # pairs, not separate texts
    assert scores == [1.0, 3.0, 2.0]  # in the order the passages were given


async def test_scores_keep_their_order_across_batches() -> None:
    reranker, tokenizer, session = _reranker(batch_size=2)

    scores = await reranker.rerank("q", ["a", "bb", "ccc", "dddd", "eeeee"])

    assert scores == [1.0, 2.0, 3.0, 4.0, 5.0]
    assert [len(batch) for batch in tokenizer.seen] == [2, 2, 1]
    assert session.calls == 3


async def test_a_flat_score_vector_is_accepted_too() -> None:
    reranker, _, _ = _reranker(shape="flat")
    assert await reranker.rerank("q", ["aa", "b"]) == [2.0, 1.0]


async def test_the_model_gets_token_types_only_when_it_asks_for_them() -> None:
    plain, _, plain_session = _reranker()
    typed, _, typed_session = _reranker(with_token_types=True)

    await plain.rerank("q", ["a"])
    await typed.rerank("q", ["a"])

    assert set(plain_session.fed) == {"input_ids", "attention_mask"}
    assert set(typed_session.fed) == {"input_ids", "attention_mask", "token_type_ids"}
    assert typed_session.fed["token_type_ids"].tolist() == [[0, 0, 0]]


async def test_no_passages_means_no_work_and_the_model_is_never_loaded() -> None:
    loads: list[int] = []

    def loader() -> tuple[Any, Any]:
        loads.append(1)
        return _FakeTokenizer(), _FakeSession()

    reranker = BgeReranker(directory=Path("unused"), loader=loader)

    assert await reranker.rerank("q", []) == []
    assert loads == []


async def test_the_model_loads_once_for_many_calls() -> None:
    loads: list[int] = []
    tokenizer, session = _FakeTokenizer(), _FakeSession()

    def loader() -> tuple[Any, Any]:
        loads.append(1)
        return tokenizer, session

    reranker = BgeReranker(directory=Path("unused"), loader=loader)
    await reranker.rerank("q", ["a"])
    await reranker.rerank("q", ["a"])

    assert loads == [1]


async def test_a_failed_load_is_reported_as_unavailable_and_retried_next_time() -> None:
    attempts: list[int] = []

    def loader() -> tuple[Any, Any]:
        attempts.append(1)
        if len(attempts) == 1:
            raise ConnectionError("no network")
        return _FakeTokenizer(), _FakeSession()

    reranker = BgeReranker(directory=Path("unused"), loader=loader)

    with pytest.raises(RerankUnavailableError, match="ConnectionError"):
        await reranker.rerank("q", ["a"])
    assert await reranker.rerank("q", ["a"]) == [1.0]  # the next call tries again


async def test_a_model_that_returns_the_wrong_shape_is_refused() -> None:
    reranker, _, _ = _reranker(shape="wrong")
    with pytest.raises(RerankUnavailableError, match="one score per passage"):
        await reranker.rerank("q", ["a", "b"])


async def test_a_crash_inside_the_model_is_wrapped() -> None:
    class Crashing(_FakeSession):
        def run(self, _outputs: None, inputs: dict[str, Any]) -> list[Any]:
            raise RuntimeError("onnx blew up")

    tokenizer = _FakeTokenizer()
    reranker = BgeReranker(directory=Path("unused"), loader=lambda: (tokenizer, Crashing()))
    with pytest.raises(RerankUnavailableError, match="RuntimeError"):
        await reranker.rerank("q", ["a"])


def test_a_batch_size_below_one_is_refused() -> None:
    with pytest.raises(ValueError, match="batch_size"):
        BgeReranker(directory=Path("unused"), batch_size=0)


def test_the_pinned_files_are_one_revision_with_full_checksums() -> None:
    assert re.fullmatch(r"[0-9a-f]{40}", bge_reranker.REVISION)
    assert bge_reranker.REVISION in bge_reranker.BASE_URL
    for pinned in (bge_reranker.MODEL_FILE, bge_reranker.TOKENIZER_FILE):
        assert re.fullmatch(r"[0-9a-f]{64}", pinned.sha256)
