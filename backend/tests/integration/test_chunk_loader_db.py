"""The chunk loader against a real Postgres with pgvector: upserts, resuming, relabelling and pruning."""

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from embedding_fakes import FakeEmbedder, fake_vector
from storyweaver.rag.loader import LoadError, load_chunks
from storyweaver.storage.db import create_engine, create_session_factory
from storyweaver.storage.models import TaleChunkRow

pytestmark = pytest.mark.integration


def _line(tale: str, position: int, body: str | None = None, **extra: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "source": "gutenberg",
        "tale_id": f"gutenberg:{tale}",
        "position": position,
        "title": f"Tale {tale}",
        "theme": None,
        "moral": None,
        "age_band": "4-6",
        "grade": 3.2,
        "licence": "public-domain-us",
        "text": body or f"Chunk {position} of tale {tale}: a small fox crossed the quiet river.",
        "words": 12,  # extra field from the cleaning step: ignored
        "cleaning_version": 1,
    }
    record.update(extra)
    return record


def _write(path: Path, records: list[dict[str, Any]], *, raw: list[str] | None = None) -> Path:
    lines = [json.dumps(record) for record in records] + (raw or [])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


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


async def _rows(factory: async_sessionmaker[AsyncSession]) -> dict[tuple[str, int], TaleChunkRow]:
    async with factory() as session:
        result = await session.execute(select(TaleChunkRow))
        return {(row.tale_id, row.position): row for row in result.scalars()}


async def test_loads_chunks_with_embeddings_and_text_search(tmp_path: Path, factory: Any) -> None:
    path = _write(tmp_path / "c.jsonl", [_line("1", 0), _line("1", 1), _line("2", 0, theme="courage")])
    embedder = FakeEmbedder()

    report = await load_chunks(path, embedder=embedder, session_factory=factory)

    assert (report.lines, report.rejected, report.inserted) == (3, 0, 3)
    rows = await _rows(factory)
    assert set(rows) == {("gutenberg:1", 0), ("gutenberg:1", 1), ("gutenberg:2", 0)}
    row = rows[("gutenberg:2", 0)]
    assert row.theme == "courage"
    assert row.licence == "public-domain-us"
    assert list(row.embedding) == pytest.approx(fake_vector(row.text), abs=1e-6)
    assert row.tsv is not None and "'fox'" in row.tsv  # Postgres filled it from the text
    assert embedder.query_calls == []  # passages never get the query instruction


async def test_a_second_run_changes_and_embeds_nothing(tmp_path: Path, factory: Any) -> None:
    path = _write(tmp_path / "c.jsonl", [_line("1", 0), _line("1", 1)])
    await load_chunks(path, embedder=FakeEmbedder(), session_factory=factory)
    before = await _rows(factory)

    embedder = FakeEmbedder()
    report = await load_chunks(path, embedder=embedder, session_factory=factory)

    assert (report.inserted, report.unchanged) == (0, 2)
    assert embedder.passage_calls == []  # the model is not even asked
    after = await _rows(factory)
    assert {key: row.id for key, row in after.items()} == {key: row.id for key, row in before.items()}


async def test_an_interrupted_run_resumes_where_it_stopped(tmp_path: Path, factory: Any) -> None:
    records = [_line("1", position) for position in range(5)]
    path = _write(tmp_path / "c.jsonl", records)
    await load_chunks(path, embedder=FakeEmbedder(), session_factory=factory, limit=2)
    assert len(await _rows(factory)) == 2

    embedder = FakeEmbedder()
    report = await load_chunks(path, embedder=embedder, session_factory=factory, batch_size=2)

    assert (report.unchanged, report.inserted) == (2, 3)
    assert sorted(embedder.passages_embedded) == sorted(str(r["text"]) for r in records[2:])
    assert len(await _rows(factory)) == 5


async def test_changed_text_is_embedded_again_and_keeps_its_row(tmp_path: Path, factory: Any) -> None:
    path = tmp_path / "c.jsonl"
    await load_chunks(
        _write(path, [_line("1", 0), _line("1", 1)]), embedder=FakeEmbedder(), session_factory=factory
    )
    before = await _rows(factory)

    embedder = FakeEmbedder()
    report = await load_chunks(
        _write(path, [_line("1", 0), _line("1", 1, "A brand new passage about a heron.")]),
        embedder=embedder,
        session_factory=factory,
    )

    assert (report.replaced, report.unchanged, report.inserted) == (1, 1, 0)
    assert embedder.passages_embedded == ["A brand new passage about a heron."]
    after = await _rows(factory)
    changed = after[("gutenberg:1", 1)]
    assert changed.id == before[("gutenberg:1", 1)].id
    assert list(changed.embedding) == pytest.approx(
        fake_vector("A brand new passage about a heron."), abs=1e-6
    )
    assert changed.tsv is not None and "'heron'" in changed.tsv  # the generated column followed the text


async def test_new_labels_update_the_row_without_embedding(tmp_path: Path, factory: Any) -> None:
    path = tmp_path / "c.jsonl"
    await load_chunks(
        _write(path, [_line("1", 0), _line("1", 1)]), embedder=FakeEmbedder(), session_factory=factory
    )
    before = await _rows(factory)

    embedder = FakeEmbedder()
    report = await load_chunks(
        _write(path, [_line("1", 0, theme="friendship", moral="Share what you have."), _line("1", 1)]),
        embedder=embedder,
        session_factory=factory,
    )

    assert (report.retagged, report.unchanged) == (1, 1)
    assert embedder.passage_calls == []
    after = await _rows(factory)
    row = after[("gutenberg:1", 0)]
    assert (row.theme, row.moral) == ("friendship", "Share what you have.")
    assert list(row.embedding) == list(before[("gutenberg:1", 0)].embedding)  # untouched


async def test_bad_lines_are_counted_and_skipped_and_good_ones_still_load(
    tmp_path: Path, factory: Any
) -> None:
    path = _write(
        tmp_path / "c.jsonl",
        [
            _line("1", 0),
            _line("1", 0, "A duplicate key."),
            _line("2", 0, age_band="teens"),
            _line("3", 0, text=""),
        ],
        raw=["{not json", "[1, 2]", "", "   "],
    )

    report = await load_chunks(path, embedder=FakeEmbedder(), session_factory=factory)

    assert report.inserted == 1
    assert (
        report.rejected == 5
    )  # duplicate, bad band, empty text, bad JSON, not an object (blank lines are not lines)
    joined = "\n".join(report.rejections)
    assert "appears more than once" in joined and "age_band" in joined and "not valid JSON" in joined
    assert set(await _rows(factory)) == {("gutenberg:1", 0)}


async def test_prune_removes_chunks_that_left_the_file_only_when_asked(tmp_path: Path, factory: Any) -> None:
    path = tmp_path / "c.jsonl"
    await load_chunks(
        _write(path, [_line("1", 0), _line("1", 1), _line("1", 2), _line("2", 0)]),
        embedder=FakeEmbedder(),
        session_factory=factory,
    )
    shorter = _write(path, [_line("1", 0)])

    kept = await load_chunks(shorter, embedder=FakeEmbedder(), session_factory=factory)
    assert kept.pruned == 0 and len(await _rows(factory)) == 4  # default: nothing is deleted

    pruned = await load_chunks(shorter, embedder=FakeEmbedder(), session_factory=factory, prune=True)
    assert pruned.pruned == 2
    # Tale 2 is not in the file at all, so it is not this run's business.
    assert set(await _rows(factory)) == {("gutenberg:1", 0), ("gutenberg:2", 0)}


async def test_prune_is_refused_after_rejections_or_with_a_limit(tmp_path: Path, factory: Any) -> None:
    path = tmp_path / "c.jsonl"
    await load_chunks(
        _write(path, [_line("1", 0), _line("1", 1)]), embedder=FakeEmbedder(), session_factory=factory
    )

    rejected = await load_chunks(
        _write(path, [_line("1", 0)], raw=["{broken"]),
        embedder=FakeEmbedder(),
        session_factory=factory,
        prune=True,
    )
    assert rejected.pruned == 0 and rejected.prune_skipped_because == "some lines were rejected"

    limited = await load_chunks(
        _write(path, [_line("1", 0)]), embedder=FakeEmbedder(), session_factory=factory, prune=True, limit=1
    )
    assert limited.pruned == 0 and limited.prune_skipped_because == "the run was limited"
    assert len(await _rows(factory)) == 2


async def test_batches_span_the_file_and_progress_is_reported(tmp_path: Path, factory: Any) -> None:
    path = _write(tmp_path / "c.jsonl", [_line("1", position) for position in range(7)])
    embedder = FakeEmbedder()
    seen: list[int] = []

    report = await load_chunks(
        path,
        embedder=embedder,
        session_factory=factory,
        batch_size=3,
        progress=lambda r: seen.append(r.loaded),
    )

    assert report.inserted == 7
    assert [len(call) for call in embedder.passage_calls] == [3, 3, 1]
    assert seen == [3, 6, 7]


async def test_a_wrong_width_embedder_is_refused_before_anything_is_written(
    tmp_path: Path, factory: Any
) -> None:
    path = _write(tmp_path / "c.jsonl", [_line("1", 0)])
    with pytest.raises(LoadError, match="384"):
        await load_chunks(path, embedder=FakeEmbedder(dimension=8), session_factory=factory)
    assert await _rows(factory) == {}


async def test_loaded_chunks_are_found_by_vector_and_by_words(tmp_path: Path, factory: Any) -> None:
    """What the retriever will do: nearest by cosine distance, and full-text match."""
    path = _write(
        tmp_path / "c.jsonl",
        [
            _line("1", 0, "A tiny dragon was afraid of the dark."),
            _line("2", 0, "The tortoise and the hare ran a race."),
            _line("3", 0, "A cobbler's elves made shoes at night."),
        ],
    )
    await load_chunks(path, embedder=FakeEmbedder(), session_factory=factory)

    async with factory() as session:
        query = fake_vector("The tortoise and the hare ran a race.")
        nearest = await session.execute(
            select(TaleChunkRow.tale_id).order_by(TaleChunkRow.embedding.cosine_distance(query)).limit(1)
        )
        assert nearest.scalar_one() == "gutenberg:2"
        matched = await session.execute(
            select(func.count()).where(TaleChunkRow.tsv.op("@@")(func.plainto_tsquery("english", "dragon")))
        )
        assert matched.scalar_one() == 1
