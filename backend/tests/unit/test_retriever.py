"""The retriever's pure parts: rank fusion, building the word query, one-chunk-per-tale, the age-band
policy and the query text. (The searches themselves run against Postgres in tests/integration.)"""

import uuid

import pytest

from storyweaver.rag.retriever import (
    RetrievalQuery,
    RetrievedChunk,
    bands_for,
    build_tsquery,
    one_per_tale,
    reciprocal_rank_fusion,
)

pytestmark = pytest.mark.unit


def test_fusion_of_one_list_keeps_its_order() -> None:
    assert [key for key, _ in reciprocal_rank_fusion([["a", "b", "c"]])] == ["a", "b", "c"]


def test_a_key_found_by_both_lists_beats_keys_found_by_one() -> None:
    fused = reciprocal_rank_fusion([["x", "shared"], ["y", "shared"]])
    assert next(key for key, _ in fused) == "shared"  # 2nd in both beats 1st in only one


def test_scores_are_one_over_k_plus_rank_added_up() -> None:
    fused = dict(reciprocal_rank_fusion([["a", "b"], ["b"]], k=60))
    assert fused["a"] == pytest.approx(1 / 61)
    assert fused["b"] == pytest.approx(1 / 62 + 1 / 61)


def test_ties_keep_the_order_of_first_appearance() -> None:
    fused = reciprocal_rank_fusion([["a", "b"], ["b", "a"]])
    assert [key for key, _ in fused] == ["a", "b"]  # equal scores: "a" was seen first


def test_fusion_of_nothing_is_nothing() -> None:
    assert reciprocal_rank_fusion([]) == []
    assert reciprocal_rank_fusion([[], []]) == []


def test_a_smaller_k_lets_the_top_rank_lead_by_more() -> None:
    sharp = dict(reciprocal_rank_fusion([["a", "b"]], k=1))
    soft = dict(reciprocal_rank_fusion([["a", "b"]], k=100))
    assert sharp["a"] / sharp["b"] > soft["a"] / soft["b"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("a shy hedgehog finds a lantern", "shy | hedgehog | finds | lantern"),
        ("Hedgehog hedgehog HEDGEHOG", "hedgehog"),
        ("tell me a little story about a dragon", "dragon"),  # request words are dropped
        ("it is so", None),  # nothing of 3+ letters left
        ("   ", None),
        ("", None),
        ("dragon's cave, 2 bears & a fox!", "dragon | cave | bears | fox"),
    ],
)
def test_the_word_query_is_an_or_of_plain_words(text: str, expected: str | None) -> None:
    assert build_tsquery(text) == expected


def test_the_word_query_can_never_carry_tsquery_syntax() -> None:
    built = build_tsquery("'); DROP TABLE tale_chunks; -- & ! ( ) <-> :* | fox")
    assert built is not None
    assert set(built) <= set("abcdefghijklmnopqrstuvwxyz0123456789 |")
    assert "fox" in built.split(" | ")


def _chunk(tale: str, position: int) -> RetrievedChunk:
    return RetrievedChunk(
        id=uuid.uuid4(),
        tale_id=tale,
        position=position,
        title=tale,
        theme=None,
        moral=None,
        age_band="4-6",
        text="x",
        score=1.0,
    )


def test_one_chunk_per_tale_keeps_the_best_ranked() -> None:
    chunks = [_chunk("a", 3), _chunk("b", 0), _chunk("a", 1), _chunk("c", 0), _chunk("b", 2)]
    kept = one_per_tale(chunks, 1)
    assert [(c.tale_id, c.position) for c in kept] == [("a", 3), ("b", 0), ("c", 0)]


def test_two_chunks_per_tale_when_asked() -> None:
    chunks = [_chunk("a", 0), _chunk("a", 1), _chunk("a", 2), _chunk("b", 0)]
    assert [(c.tale_id, c.position) for c in one_per_tale(chunks, 2)] == [("a", 0), ("a", 1), ("b", 0)]


def test_a_4_to_6_story_may_draw_on_one_band_up_but_not_the_hardest() -> None:
    assert bands_for("4-6") == ("4-6", "7-9")
    assert "10+" not in bands_for("4-6")


def test_other_bands_get_only_themselves() -> None:
    assert bands_for("7-9") == ("7-9",)
    assert bands_for("10+") == ("10+",)


def test_the_search_text_is_the_idea_plus_what_was_extracted() -> None:
    query = RetrievalQuery(
        idea="a shy hedgehog",
        themes=["courage", "friendship"],
        characters=["hedgehog"],
        moral="Be brave.",
        setting="a night garden",
    )
    assert query.search_text() == (
        "a shy hedgehog Themes: courage, friendship. Characters: hedgehog. "
        "Moral: Be brave. Setting: a night garden."
    )


def test_the_search_text_of_a_bare_idea_is_the_idea() -> None:
    assert RetrievalQuery(idea="  a shy hedgehog ").search_text() == "a shy hedgehog"
    assert RetrievalQuery(idea="   ").search_text() == ""
