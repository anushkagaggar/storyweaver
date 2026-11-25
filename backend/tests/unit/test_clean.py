"""data_pipeline/flows/clean.py: raw Gutenberg ebooks -> clean, scene-sized, graded chunks (V4).

The texts are built here in Gutenberg's own style (licence header and footer, credits, chapter headings,
hard-wrapped lines, [Illustration] tags, CRLF line ends), so each step is tested on what it will really meet.
"""

import importlib.util
import json
import random
import sys
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_PATH = Path(__file__).resolve().parents[3] / "data_pipeline" / "flows" / "clean.py"
_spec = importlib.util.spec_from_file_location("clean_flow", _PATH)
assert _spec is not None and _spec.loader is not None
clean = importlib.util.module_from_spec(_spec)
sys.modules["clean_flow"] = clean
_spec.loader.exec_module(clean)

ANIMALS = ["fox", "hare", "owl", "mole", "wren", "badger", "goose", "otter", "stoat", "robin"]
NOUNS = ["lantern", "acorn", "kettle", "feather", "pebble", "ribbon", "basket", "button", "clover", "teacup"]
PLACES = ["meadow", "bridge", "orchard", "hill", "pond", "cottage", "garden", "lane", "mill", "river"]


def _name(i: int) -> str:
    """A made-up one-word name, different for every i, so no two sentences share their 5-word phrases."""
    return "Zo" + "".join(chr(97 + (i // 26**k) % 26) for k in range(3))


def _sentence(i: int) -> str:
    """25 words of plain English."""
    return (
        f"On that day {_name(i)} the little {ANIMALS[i % 10]} found a {NOUNS[(i // 3) % 10]} by the old "
        f"{PLACES[(i // 7) % 10]}, and {_name(i)} ran home to tell her mother all about it."
    )


def _paragraph(start: int, count: int) -> str:
    return " ".join(_sentence(i) for i in range(start, start + count))


def _ebook(paragraph_list: list[str], *, title: str = "A Small Tale", markers: bool = True) -> str:
    """Text as Gutenberg serves it: licence, credits, heading, wrapped lines, illustration tag, footer."""
    body = "\n\n".join(textwrap.fill(p, width=70) for p in paragraph_list)
    head = (
        f"The Project Gutenberg eBook of {title}\n\n"
        "This eBook is for the use of anyone anywhere in the United States and most other parts "
        "of the world\n"
        "at no cost and with almost no restrictions whatsoever.\n\n"
    )
    start = f"*** START OF THE PROJECT GUTENBERG EBOOK {title.upper()} ***\n\n" if markers else ""
    front = (
        f"Produced by Some Kind Volunteers\n\n{title.upper()}\n\nCHAPTER I\n\n"
        "[Illustration: a little fox\nrunning home]\n\n"
    )
    foot = (
        f"\n\n*** END OF THE PROJECT GUTENBERG EBOOK {title.upper()} ***\n\n"
        "Section 1. General Terms of Use and Redistributing Project Gutenberg electronic works\n"
    )
    return (head + start + front + body + foot).replace("\n", "\r\n")


# ----------------------------------------------------------------------------------- [1] normalising


def test_normalise_fixes_unicode_whitespace_and_plain_text_markup() -> None:
    raw = (
        "The \ufb01sh\u00a0was\u200b _very_ big--and snake_case_word stays.\r\n"  # ligature, NBSP, zero-width
        "[Illustration: a fox\nrunning] Done.[12]"
    )

    text = clean.normalise(raw)

    assert "fish was very big—and snake_case_word stays." in text  # ligature, NBSP, zero-width, italics, dash
    assert "\r" not in text
    assert "Illustration" not in text
    assert "[12]" not in text
    assert text.endswith("Done.")


def test_boilerplate_is_cut_at_the_modern_markers() -> None:
    raw = (
        "Licence...\n*** START OF THE PROJECT GUTENBERG EBOOK X ***\nThe story.\n"
        "*** END OF THE PROJECT GUTENBERG EBOOK X ***\nMore licence..."
    )

    assert clean.strip_boilerplate(raw).strip() == "The story."


def test_boilerplate_is_cut_at_the_old_markers_and_this_variant() -> None:
    old = (
        "Small print\n*END*THE SMALL PRINT! FOR PUBLIC DOMAIN ETEXTS*Ver.04.29.93*END*\nThe story.\n"
        "End of the Project Gutenberg Etext of X\nlicence"
    )
    variant = "x\n*** start of this project gutenberg ebook x ***\nThe story.\n"

    assert clean.strip_boilerplate(old).strip() == "The story."
    assert clean.strip_boilerplate(variant).strip() == "The story."  # no end marker: keeps to the end


def test_a_text_without_a_start_marker_is_refused_rather_than_guessed() -> None:
    assert clean.strip_boilerplate("Once upon a time. The end.") is None
    with pytest.raises(clean.CleanError, match="marker"):
        clean.clean_text(_ebook([_paragraph(0, 5)], markers=False))


def test_paragraphs_rejoin_wrapped_lines_and_drop_what_is_not_prose() -> None:
    toc = " ".join(f"Chapter{n} The Tale Number {n}" for n in range(8))  # 40+ words, no sentence marks
    body = (
        "Produced by Some Volunteers\n\nTHE FROG PRINCE\n\nCHAPTER I\n\n"
        f"{textwrap.fill(_paragraph(0, 2), width=40)}\n\n{toc}\n\nContents\n\n{_paragraph(10, 1)}"
    )

    found = clean.paragraphs(body)

    assert found == [_paragraph(0, 2), _paragraph(10, 1)]
    assert all("\n" not in p for p in found)


# ----------------------------------------------------------------------------------- sentences, scenes


def test_sentences_do_not_split_at_abbreviations_initials_or_inside_dialogue() -> None:
    assert clean.split_sentences("Mr. Fox said hello to Mrs. Hen. He left.") == [
        "Mr. Fox said hello to Mrs. Hen.",
        "He left.",
    ]
    assert len(clean.split_sentences("J. M. Barrie wrote it. Yes.")) == 2
    assert clean.split_sentences('"Go away!" said the troll. "No," said Pip.') == [
        '"Go away!" said the troll.',
        '"No," said Pip.',
    ]


def _sizes(scenes: list[str]) -> list[int]:
    return [len(scene.split()) for scene in scenes]


def test_scenes_end_at_paragraph_ends_once_they_are_long_enough() -> None:
    paragraph_list = [_paragraph(i * 5, 5) for i in range(6)]  # six paragraphs of 125 words

    assert _sizes(clean.split_scenes(paragraph_list)) == [125] * 6


def test_a_long_paragraph_is_cut_at_sentence_ends_below_the_maximum() -> None:
    scenes = clean.split_scenes([_paragraph(0, 12)])  # 300 words in one paragraph

    assert _sizes(scenes) == [150, 150]
    assert all(scene.endswith("it.") for scene in scenes)  # every scene ends where a sentence ends


def test_a_short_tail_is_shared_out_evenly_not_left_tiny() -> None:
    # Eight 25-word sentences: cutting greedily gives 150 + 50; sharing the tail gives 100 + 100.
    assert _sizes(clean.split_scenes([_paragraph(0, 8)])) == [100, 100]


def test_a_short_tail_that_fits_is_merged() -> None:
    assert _sizes(clean.split_scenes([_paragraph(0, 4), _paragraph(4, 2)])) == [150]  # 100 + 50


def test_a_tiny_tale_is_one_scene_and_a_giant_sentence_stands_alone() -> None:
    assert _sizes(clean.split_scenes([_paragraph(0, 2)])) == [50]
    giant = "He walked " + "and walked " * 100 + "home."
    scenes = clean.split_scenes([_paragraph(0, 4), giant])
    assert _sizes(scenes)[-1] > clean.MAX_WORDS
    assert not clean.looks_like_prose(scenes[-1])


def test_no_word_is_lost_or_repeated_whatever_the_shape_of_the_text() -> None:
    rng = random.Random(7)  # noqa: S311 (a repeatable shape for the text, not security)
    vocabulary = "the a and then went home to see her little fox said which was very old green hill".split()
    paragraph_list = []
    for _ in range(60):
        sentences = [
            " ".join(rng.choices(vocabulary, k=rng.randint(5, 40))).capitalize() + "."
            for _ in range(rng.randint(1, 9))
        ]
        paragraph_list.append(" ".join(sentences))

    scenes = clean.split_scenes(paragraph_list)

    assert " ".join(scenes).split() == " ".join(paragraph_list).split()
    sizes = _sizes(scenes)
    assert max(sizes) <= clean.MAX_WORDS
    assert min(sizes) >= 60
    assert sum(clean.MIN_WORDS <= s <= clean.MAX_WORDS for s in sizes) / len(sizes) > 0.9


# ----------------------------------------------------------------------------------- checks per chunk


def test_english_is_told_from_french_and_from_lists_of_names() -> None:
    english = _paragraph(0, 3)
    french = (
        "Il était une fois un petit renard qui vivait dans une grande forêt avec sa mère. "
        "Chaque matin, il allait chercher des fruits près de la rivière, puis il rentrait à la maison."
    )

    assert clean.looks_like_english(english)
    assert not clean.looks_like_english(french)
    assert not clean.looks_like_english("")


@pytest.mark.parametrize(
    ("grade", "band"),
    [(-1.0, "4-6"), (3.9, "4-6"), (4.0, "4-6"), (4.1, "7-9"), (7.0, "7-9"), (7.1, "10+"), (14.0, "10+")],
)
def test_grade_becomes_an_age_band(grade: float, band: str) -> None:
    assert clean.age_band_for(grade) == band


@pytest.mark.parametrize(
    "word", ["murdered", "slaughter", "stabbed", "beheaded", "corpse", "gallows", "bloody", "suicide"]
)
def test_one_severe_word_drops_the_chunk(word: str) -> None:
    assert clean.is_unsafe(f"The king was {word} that night, said the old woman.")


def test_mild_words_only_count_in_numbers_and_stems_are_not_matched_loosely() -> None:
    assert not clean.is_unsafe("The wolf was killed by the hunter.")  # one mild word is a tale, not a warning
    assert clean.is_unsafe("He killed it. It was dead. Its blood was on the grave.")  # four
    assert not clean.is_unsafe("The horse stood in the stable by the grapes.")  # 'stab' inside 'stable'


def test_licence_text_is_recognised_in_a_chunk() -> None:
    assert clean.leaks_licence("Section 1. General Terms of Use and Redistributing Project Gutenberg works")
    assert not clean.leaks_licence(_paragraph(0, 2))


# ----------------------------------------------------------------------------------- [3] duplicates


def test_a_copy_and_a_lightly_edited_copy_are_dropped_but_a_different_chunk_is_kept() -> None:
    index = clean.NearDuplicateIndex()
    original = _paragraph(0, 5)
    edited = original.replace("mother", "father", 1)  # one word of ~125 changed

    assert index.add_if_new(original)
    assert not index.add_if_new(original)
    assert not index.add_if_new(edited)
    assert index.add_if_new(_paragraph(100, 5))


def test_the_duplicate_check_is_deterministic_and_handles_tiny_text() -> None:
    first, second = clean.NearDuplicateIndex(), clean.NearDuplicateIndex()
    texts = [_paragraph(0, 5), "Hi.", _paragraph(0, 5), "Hi."]

    assert (
        [first.add_if_new(t) for t in texts]
        == [second.add_if_new(t) for t in texts]
        == [True, True, False, False]
    )


# ----------------------------------------------------------------------------------- the whole flow


def _write_raw(raw: Path, tales: dict[int, str | None]) -> None:
    """tales: Gutenberg id -> ebook text (None: listed in tales.jsonl but its text was never fetched)."""
    (raw / "texts").mkdir(parents=True)
    records = []
    for number, text in tales.items():
        records.append(
            {
                "source": "gutenberg",
                "tale_id": f"gutenberg:{number}",
                "gutenberg_id": number,
                "title": f"Tale {number}",
                "licence": "public-domain-us",
            }
        )
        if text is not None:
            (raw / "texts" / f"{number}.txt").write_bytes(text.encode("utf-8"))
    (raw / "tales.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


FRENCH = " ".join(
    f"Il était une fois un petit renard numéro{i} qui vivait dans une grande forêt avec sa mère et ses amis."
    for i in range(6)  # 120 words: one chunk
)
VIOLENT = (
    "The wicked king murdered his brother in the dark, and the old woman wept for him all night long. " * 6
)


def test_the_flow_end_to_end_with_every_kind_of_trouble(tmp_path: Path) -> None:
    good = _ebook([_paragraph(0, 5), _paragraph(5, 5), _paragraph(10, 5)], title="Good Tale")
    _write_raw(
        tmp_path / "raw",
        {
            1: good,
            2: _ebook([_paragraph(40, 5)], markers=False),  # no header marker
            3: good,  # the same book again under another number: a second edition
            4: _ebook([FRENCH], title="Conte"),
            5: _ebook([VIOLENT, _paragraph(60, 5)], title="Grim Tale"),
            6: None,  # never fetched
        },
    )

    counts = clean.clean_corpus(tmp_path / "raw", tmp_path / "clean")

    assert counts["tales_in"] == 6
    assert counts["tales_no_text_file"] == 1
    assert counts["tales_skipped_no_marker"] == 1
    assert counts["tales_cleaned"] == 4
    assert counts["dropped_near_duplicate"] == 3  # all of tale 3
    assert counts["dropped_not_english"] == 1
    assert counts["dropped_unsafe"] == 1
    assert counts["dropped_licence_text"] == 0  # the header, footer and credits never reached a chunk
    assert counts["chunks_kept"] == 4  # three from tale 1, one from tale 5

    records = [
        json.loads(line)
        for line in (tmp_path / "clean" / "chunks.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [(r["tale_id"], r["position"]) for r in records] == [
        ("gutenberg:1", 0),
        ("gutenberg:1", 1),
        ("gutenberg:1", 2),
        ("gutenberg:5", 1),  # position 0 was the violent chunk: positions keep their gap
    ]
    first = records[0]
    assert first["source"] == "gutenberg"
    assert first["licence"] == "public-domain-us"
    assert first["theme"] is None
    assert first["age_band"] in {"4-6", "7-9", "10+"}
    assert first["words"] == 125
    assert first["cleaning_version"] == clean.CLEANING_VERSION
    assert "Gutenberg" not in first["text"]
    assert "\r" not in first["text"]
    assert "Illustration" not in first["text"]
    assert len({(r["tale_id"], r["position"]) for r in records}) == len(records)  # what the database requires

    meta = json.loads((tmp_path / "clean" / "chunks.meta.json").read_text(encoding="utf-8"))
    assert meta["counts"]["chunks_kept"] == 4
    assert meta["cleaning_version"] == clean.CLEANING_VERSION


def test_running_the_flow_twice_gives_the_same_chunks(tmp_path: Path) -> None:
    _write_raw(tmp_path / "raw", {1: _ebook([_paragraph(0, 5), _paragraph(5, 5)])})

    clean.clean_corpus(tmp_path / "raw", tmp_path / "a")
    clean.clean_corpus(tmp_path / "raw", tmp_path / "b")

    assert (tmp_path / "a" / "chunks.jsonl").read_bytes() == (tmp_path / "b" / "chunks.jsonl").read_bytes()


def test_main_says_so_when_there_is_nothing_to_clean_yet(tmp_path: Path) -> None:
    assert clean.main(["--raw", str(tmp_path / "nothing"), "--out", str(tmp_path / "out")]) == 4


def test_main_runs_the_flow(tmp_path: Path) -> None:
    _write_raw(tmp_path / "raw", {1: _ebook([_paragraph(0, 5)])})

    assert clean.main(["--raw", str(tmp_path / "raw"), "--out", str(tmp_path / "out")]) == 0
    assert (tmp_path / "out" / "chunks.jsonl").exists()
