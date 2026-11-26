"""The chunk loader's parts that need no database: reading and checking lines, the refusal of a wrong-width
embedder, and the command-line script's exit codes. (Upserts: tests/integration/test_chunk_loader_db.py.)"""

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from embedding_fakes import FakeEmbedder
from storyweaver.rag.loader import ChunkRecord, LoadError, load_chunks, read_chunks

pytestmark = pytest.mark.unit

_PATH = Path(__file__).resolve().parents[3] / "data_pipeline" / "flows" / "load_chunks.py"
_spec = importlib.util.spec_from_file_location("load_chunks_flow", _PATH)
assert _spec is not None and _spec.loader is not None
flow = importlib.util.module_from_spec(_spec)
sys.modules["load_chunks_flow"] = flow
_spec.loader.exec_module(flow)

GOOD: dict[str, Any] = {
    "source": "gutenberg",
    "tale_id": "gutenberg:11",
    "position": 0,
    "title": "Alice",
    "theme": None,
    "moral": None,
    "age_band": "7-9",
    "grade": 5.5,
    "licence": "public-domain-us",
    "text": "Alice was beginning to get very tired of sitting by her sister on the bank.",
}


def _file(tmp_path: Path, *lines: str) -> Path:
    path = tmp_path / "chunks.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_a_good_line_becomes_a_record_and_extra_fields_are_ignored(tmp_path: Path) -> None:
    path = _file(tmp_path, json.dumps({**GOOD, "words": 14, "cleaning_version": 1}))
    [(number, item)] = list(read_chunks(path))
    assert number == 1
    assert isinstance(item, ChunkRecord)
    assert item.key == ("gutenberg:11", 0)
    assert item.labels()["age_band"] == "7-9"


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"source": "blog"}, "source"),
        ({"age_band": "teens"}, "age_band"),
        ({"position": -1}, "position"),
        ({"tale_id": ""}, "tale_id"),
        ({"tale_id": "x" * 97}, "tale_id"),
        ({"title": "t" * 201}, "title"),
        ({"theme": "t" * 41}, "theme"),
        ({"text": ""}, "text"),
        ({"text": "   \n "}, "blank"),
        ({"text": "bad \x00 byte"}, "NUL"),
        ({"grade": "easy"}, "grade"),
    ],
)
def test_bad_values_are_rejected_with_a_reason(tmp_path: Path, change: dict[str, Any], expected: str) -> None:
    path = _file(tmp_path, json.dumps({**GOOD, **change}))
    [(_, item)] = list(read_chunks(path))
    assert isinstance(item, str)
    assert expected in item


def test_a_missing_field_is_rejected_and_named(tmp_path: Path) -> None:
    without = {key: value for key, value in GOOD.items() if key != "licence"}
    [(_, item)] = list(read_chunks(_file(tmp_path, json.dumps(without))))
    assert isinstance(item, str) and item.startswith("licence")


def test_broken_json_and_non_objects_are_rejected_and_blank_lines_skipped(tmp_path: Path) -> None:
    path = _file(tmp_path, "{oops", "", "   ", "[1]", "42")
    results = list(read_chunks(path))
    assert [number for number, _ in results] == [1, 4, 5]
    assert all(isinstance(item, str) for _, item in results)
    assert "not valid JSON" in str(results[0][1])


async def test_the_wrong_embedder_width_is_refused_before_the_database_is_touched(tmp_path: Path) -> None:
    path = _file(tmp_path, json.dumps(GOOD))
    with pytest.raises(LoadError, match="384"):
        await load_chunks(path, embedder=FakeEmbedder(dimension=8), session_factory=None)  # type: ignore[arg-type]


async def test_a_zero_batch_size_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="batch_size"):
        await load_chunks(
            _file(tmp_path, json.dumps(GOOD)),
            embedder=FakeEmbedder(),
            session_factory=None,  # type: ignore[arg-type]
            batch_size=0,
        )


def test_the_target_is_described_without_its_password() -> None:
    described = flow.describe_target("postgresql://user:s3cret@ep-cool-1.neon.tech/neondb?sslmode=require")
    assert described == "ep-cool-1.neon.tech:5432/neondb"
    assert "s3cret" not in described


def test_no_database_url_exits_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(flow, "_settings", lambda: None)
    assert flow.main(["--chunks", str(_file(tmp_path, json.dumps(GOOD)))]) == 2


def test_a_missing_chunks_file_exits_3(tmp_path: Path) -> None:
    code = flow.main(["--chunks", str(tmp_path / "nope.jsonl"), "--database-url", "postgresql://u:p@h/d"])
    assert code == 3


@pytest.mark.parametrize(
    ("rejected", "expected_code"),
    [(0, 0), (2, 5)],
)
def test_exit_code_reflects_rejected_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rejected: int, expected_code: int
) -> None:
    report = flow.LoadReport(lines=3, rejected=rejected, inserted=1)

    async def fake_run(args: Any, database_url: str) -> Any:
        return report

    monkeypatch.setattr(flow, "run", fake_run)
    code = flow.main(
        ["--chunks", str(_file(tmp_path, json.dumps(GOOD))), "--database-url", "postgresql://u:p@h/d"]
    )
    assert code == expected_code


def test_a_database_or_embedder_failure_exits_6(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def failing_run(args: Any, database_url: str) -> Any:
        raise LoadError("boom")

    monkeypatch.setattr(flow, "run", failing_run)
    code = flow.main(
        ["--chunks", str(_file(tmp_path, json.dumps(GOOD))), "--database-url", "postgresql://u:p@h/d"]
    )
    assert code == 6
