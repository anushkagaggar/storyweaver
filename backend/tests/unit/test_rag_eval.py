"""evaluation/rag_eval.py: the gold set's own consistency, relevance judging, scoring and the report. No
database: the retrieval run itself is tested in tests/integration/test_rag_eval_db.py."""

import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PATH = Path(__file__).resolve().parents[3] / "evaluation" / "rag_eval.py"
_spec = importlib.util.spec_from_file_location("rag_eval_unit", _PATH)
assert _spec is not None and _spec.loader is not None
ev: Any = importlib.util.module_from_spec(_spec)
sys.modules["rag_eval_unit"] = ev
_spec.loader.exec_module(ev)

GOLD = ev.load_gold()


def _line(
    qid: str = "q1", kind: str = "named", query: str = "a hungry wolf at the door", **extra: Any
) -> str:
    raw: dict[str, Any] = {"id": qid, "kind": kind, "query": query, "all_of": ["wolf"]}
    raw.update(extra)
    return json.dumps(raw)


def _chunk(cid: int, tale: str, band: str, text: str) -> Any:
    return ev.CorpusChunk(id=cid, tale_id=tale, title=tale.title(), age_band=band, text=text)


# ---------------------------------------------------------------------------------------------- the gold file


def test_the_gold_set_is_fifty_requests_with_unique_ids() -> None:
    assert len(GOLD) == 50
    assert len({q.id for q in GOLD}) == 50
    assert len({q.query.lower() for q in GOLD}) == 50


def test_the_gold_set_mixes_named_and_described_requests() -> None:
    kinds = {kind: sum(q.kind == kind for q in GOLD) for kind in ev.KINDS}
    assert kinds == {"named": 18, "described": 32}


def test_every_request_reads_like_a_child_asking() -> None:
    for q in GOLD:
        assert 5 <= len(q.query.split()) <= 25, q.id


def test_a_described_request_shares_no_pattern_with_its_tale() -> None:
    """Otherwise the word search would find it trivially and "described" would measure nothing."""
    leaks = [q.id for q in GOLD if q.kind == "described" for p in q.patterns if p.search(q.query)]
    assert leaks == []


@pytest.mark.parametrize(
    ("qid", "passage", "expected"),
    [
        ("q01", "I'll huff, and I'll puff, and I'll blow your house in!", True),
        ("q01", "The wolf blew and blew, but the house of bricks stood.", False),  # no huff/puff
        ("q27", "The Ant and the Grasshopper: all summer the grasshopper sang.", True),
        ("q27", "I want the grasshopper to sing, said the farmer.", False),  # "want" is not "ant"
        ("q17", "Snow-White lived with the seven dwarfs", True),
        ("q17", "Snow White met a dwarf in the wood", True),
        ("q30", "The Country Mouse invited his cousin", True),
        ("q33", "Pinocchio's nose grew longer", True),
        ("q33", "A puppet's nose grew longer", False),
    ],
)
def test_the_patterns_match_the_passages_they_are_meant_for(qid: str, passage: str, expected: bool) -> None:
    query = next(q for q in GOLD if q.id == qid)
    assert query.matches(passage) is expected


# -------------------------------------------------------------------------------------------- parse_gold


def test_a_request_needs_every_pattern_in_the_same_passage() -> None:
    line = json.dumps({"id": "a", "kind": "named", "query": "x y z", "all_of": ["wolf", "pig"]})
    query = ev.parse_gold([line])[0]
    assert query.matches("The WOLF met a pig")
    assert not query.matches("The wolf met a goat")


@pytest.mark.parametrize(
    "bad",
    [
        "not json",
        json.dumps({"id": "a", "kind": "named", "query": "x"}),  # no all_of
        json.dumps({"id": "a", "kind": "vague", "query": "x", "all_of": ["x"]}),  # unknown kind
        json.dumps({"id": "a", "kind": "named", "query": "x", "all_of": []}),
        json.dumps({"id": "a", "kind": "named", "query": "x", "all_of": [""]}),
        json.dumps({"id": "a", "kind": "named", "query": "x", "all_of": ["(unclosed"]}),
        json.dumps({"id": "a", "kind": "named", "query": "  ", "all_of": ["x"]}),
        json.dumps({"id": "", "kind": "named", "query": "x", "all_of": ["x"]}),
    ],
)
def test_a_malformed_gold_line_is_refused_with_its_line_number(bad: str) -> None:
    with pytest.raises(ev.GoldError, match="gold line 2"):
        ev.parse_gold([_line(), bad])


def test_duplicate_ids_are_refused() -> None:
    with pytest.raises(ev.GoldError, match="duplicate id 'q1'"):
        ev.parse_gold([_line(), _line()])


def test_blank_lines_and_comments_are_ignored() -> None:
    assert len(ev.parse_gold(["", "# a note", _line()])) == 1


# ---------------------------------------------------------------------------------------------- judging


def test_a_request_is_judged_only_when_a_usable_passage_exists() -> None:
    queries = ev.parse_gold(
        [
            _line("in", query="a hungry wolf at the door", all_of=["wolf"]),
            _line("missing", query="a ship in a storm", all_of=["ship"]),
            _line("old", query="a dragon in the cave", all_of=["dragon"]),
        ]
    )
    corpus = [
        _chunk(1, "pigs", "4-6", "The wolf huffed."),
        _chunk(2, "lang", "10+", "The dragon guarded the cave."),
        _chunk(3, "grimm", "7-9", "A wolf came."),
    ]

    judged = {j.query.id: j for j in ev.judge(queries, corpus, "4-6")}

    assert judged["in"].status == "judged"
    assert judged["in"].relevant == {1, 3}  # a 4-6 story may draw on 7-9 as well
    assert judged["in"].tales == 2
    assert judged["missing"].status == "not_in_corpus"
    assert judged["old"].status == "filtered_by_age_band"
    assert judged["old"].matching == 1 and judged["old"].relevant == frozenset()


def test_the_age_band_decides_which_passages_count() -> None:
    queries = ev.parse_gold([_line(all_of=["wolf"])])
    corpus = [_chunk(1, "a", "4-6", "wolf"), _chunk(2, "b", "7-9", "wolf"), _chunk(3, "c", "10+", "wolf")]

    assert ev.judge(queries, corpus, "4-6")[0].relevant == {1, 2}
    assert ev.judge(queries, corpus, "10+")[0].relevant == {3}  # other bands get only themselves


# ---------------------------------------------------------------------------------------------- scoring


@pytest.mark.parametrize(
    ("ranked", "k", "expected"),
    [
        ([9, 8, 7], 5, None),
        ([9, 1, 7], 5, 2),
        ([1, 2], 5, 1),
        ([9, 8, 7, 6, 5, 1], 5, None),
        ([9, 8, 7, 6, 1], 5, 5),
    ],
)
def test_first_relevant_rank_stops_at_k(ranked: list[int], k: int, expected: int | None) -> None:
    assert ev.first_relevant_rank(ranked, frozenset({1}), k) == expected


def test_recall_and_mrr_count_a_miss_as_zero() -> None:
    score = ev.Score()
    for rank in (1, 2, None, 4):
        score.add(rank)

    assert score.judged == 4 and score.hits == 3 and score.first_place == 1
    assert score.recall == 0.75
    assert score.mrr == pytest.approx((1 + 1 / 2 + 0 + 1 / 4) / 4)


def test_an_empty_score_is_zero_not_an_error() -> None:
    assert ev.Score().recall == 0.0 and ev.Score().mrr == 0.0


def test_results_split_by_kind() -> None:
    result = ev.ConfigResult("x")
    named, described = ev.parse_gold(
        [_line("a", "named"), _line("b", "described", query="a girl who is lost")]
    )

    result.record(named, 1, [])
    result.record(described, None, [])

    assert result.overall.judged == 2
    assert result.by_kind["named"].recall == 1.0
    assert result.by_kind["described"].recall == 0.0
    assert result.ranks == {"a": 1, "b": None}


# ------------------------------------------------------------------------------------------------ report


def _report(judged: int, hits: int, *, missing: int = 0) -> tuple[str, list[Any]]:
    """A report over `judged` scoreable requests (the first `hits` found) and `missing` not in the corpus."""
    corpus = [_chunk(1, "pigs", "4-6", "A wolf came to the door.")]
    queries = ev.parse_gold([_line(f"q{i}", query=f"request number {i} for a wolf") for i in range(judged)])
    absent = ev.parse_gold([_line(f"m{i}", all_of=["xyzzy"]) for i in range(missing)])
    judgements = ev.judge([*queries, *absent], corpus, "4-6")
    result = ev.ConfigResult("hybrid, 1 passage per book")
    for index, j in enumerate(j for j in judgements if j.status == "judged"):
        result.record(j.query, 1 if index < hits else None, ["Pigs: A wolf came"])
    return ev.render_summary(judgements, [result], corpus=corpus, age_band="4-6", k=5), judgements


def test_the_report_passes_at_80_percent_of_a_fair_number_of_requests() -> None:
    summary, _ = _report(10, 8)
    assert "**Result:** PASS" in summary
    assert "recall@5 | MRR@5" in summary


def test_the_report_fails_below_the_target() -> None:
    summary, _ = _report(10, 7)
    assert "**Result:** FAIL" in summary
    assert "## Misses" in summary and "request number 9" in summary  # a miss is listed with its top results


def test_the_report_is_inconclusive_when_most_requests_are_not_in_the_corpus() -> None:
    summary, _ = _report(3, 3, missing=7)
    assert "INCONCLUSIVE (only 3 of 10 requests are in the corpus)" in summary
    assert "7 not in the corpus" in summary


def test_a_kind_with_no_judged_requests_reads_n_a_not_zero_percent() -> None:
    summary, _ = _report(10, 8)  # every request in this report is "named"

    assert "| n/a |" in summary
    assert "0% (0/0)" not in summary


def test_the_report_without_judged_requests_says_not_measured() -> None:
    summary = ev.render_summary([], [], corpus=[], age_band="4-6", k=5)
    assert "NOT MEASURED" in summary


def test_the_audit_flags_a_very_loose_pattern() -> None:
    queries = ev.parse_gold([_line(all_of=["the"])])
    corpus = [_chunk(i, f"t{i}", "4-6", "the end") for i in range(ev.TOO_LOOSE + 1)]
    judgements = ev.judge(queries, corpus, "4-6")

    summary = ev.render_summary(judgements, [], corpus=corpus, age_band="4-6", k=5)

    assert "very loose pattern" in summary


def test_results_are_written_as_jsonl_and_markdown(tmp_path: Path) -> None:
    summary, judgements = _report(2, 1)
    result = ev.ConfigResult("a")
    result.record(judgements[0].query, 2, [])

    ev.write_results(judgements, [result], summary, jsonl=tmp_path / "out" / "r.jsonl", md=tmp_path / "r.md")

    rows = [
        json.loads(line) for line in (tmp_path / "out" / "r.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [row["status"] for row in rows] == ["judged", "judged"]
    assert rows[0]["rank"] == {"a": 2}
    assert (tmp_path / "r.md").read_text(encoding="utf-8").startswith("# V4 retrieval eval")


# -------------------------------------------------------------------------------------------------- the CLI


def test_the_cli_needs_a_database(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)

    assert ev.main([]) == 2
    assert "No database" in capsys.readouterr().out


def test_a_malformed_gold_file_stops_the_run_before_any_connection(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gold = tmp_path / "gold.jsonl"
    gold.write_text("not json\n", encoding="utf-8")

    code = ev.main(["--gold", str(gold), "--database-url", "postgresql+asyncpg://nobody@127.0.0.1:1/none"])

    assert code == 4
    assert "gold line 1" in capsys.readouterr().out


def test_the_default_gold_file_is_the_one_in_the_repo() -> None:
    assert ev.GOLD.name == "rag_gold_v1.jsonl" and ev.GOLD.is_file()
    assert re.fullmatch(r"q\d\d", GOLD[0].id)
