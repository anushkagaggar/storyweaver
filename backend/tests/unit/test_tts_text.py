import pytest

from storyweaver.domain.story import NARRATOR
from storyweaver.speech.tts import ORPHEUS_MAX_CHARS, split_for_tts
from storyweaver.speech.voices import (
    CATALOGUE,
    KOKORO_VOICES,
    NARRATOR_VOICE,
    ORPHEUS_VOICES,
    assign_voices,
    voice,
    with_guest_voices,
)

pytestmark = pytest.mark.unit


def test_short_text_is_one_chunk() -> None:
    assert split_for_tts("Pip ran home.") == ["Pip ran home."]


def test_blank_text_has_no_chunks() -> None:
    assert split_for_tts("  \n ") == []


def test_sentences_are_packed_up_to_the_limit_and_never_cut() -> None:
    text = " ".join(f"Sentence number {i} is here." for i in range(40))
    chunks = split_for_tts(text)
    assert all(len(chunk) <= ORPHEUS_MAX_CHARS for chunk in chunks)
    assert all(chunk.endswith(".") for chunk in chunks)
    assert " ".join(chunks) == text
    assert len(chunks) == pytest.approx(len(text) / ORPHEUS_MAX_CHARS, abs=2)  # packed, not one per sentence


def test_a_long_sentence_breaks_at_commas_then_words() -> None:
    long_sentence = ", ".join(["the little fox ran over the hill"] * 12) + "."
    chunks = split_for_tts(long_sentence)
    assert len(chunks) > 1
    assert all(len(chunk) <= ORPHEUS_MAX_CHARS for chunk in chunks)
    assert " ".join(chunks).split() == long_sentence.split()

    no_commas = " ".join(["word"] * 120) + "."
    assert all(len(chunk) <= ORPHEUS_MAX_CHARS for chunk in split_for_tts(no_commas))


def test_closing_quotes_stay_with_their_sentence() -> None:
    assert split_for_tts("“Hello!” Pip said.", max_chars=12) == ["“Hello!”", "Pip said."]


def test_whitespace_is_normalised() -> None:
    assert split_for_tts("Pip   ran.\n\nHe  smiled.") == ["Pip ran. He smiled."]


def test_a_pathological_word_is_still_split() -> None:
    assert all(len(chunk) <= 50 for chunk in split_for_tts("x" * 130, max_chars=50))


# --- voices ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("engine", ["orpheus", "kokoro"])
def test_narrator_has_the_engine_narrator_voice_and_characters_get_distinct_voices(engine: str) -> None:
    voices = assign_voices(engine, ["pip", "olly", "mama_owl"], story_key="story-1")  # type: ignore[arg-type]
    assert voices[NARRATOR] == NARRATOR_VOICE[engine]  # type: ignore[index]
    character_voices = [voices[c] for c in ("pip", "olly", "mama_owl")]
    assert len(set(character_voices)) == 3
    assert NARRATOR_VOICE[engine] not in character_voices  # type: ignore[index]
    catalogue_ids = {v.id for v in CATALOGUE[engine]}  # type: ignore[index]
    assert set(voices.values()) <= catalogue_ids


def test_assignment_is_stable_per_story_and_varies_between_stories() -> None:
    characters = ["pip", "olly"]
    assert assign_voices("kokoro", characters, "a") == assign_voices(
        "kokoro", list(reversed(characters)), "a"
    )
    across = {tuple(assign_voices("kokoro", characters, f"story-{i}").values()) for i in range(20)}
    assert len(across) > 1


def test_more_characters_than_voices_reuse_voices() -> None:
    characters = [f"c{i}" for i in range(7)]
    voices = assign_voices("orpheus", characters, "s")
    assert len(voices) == 8 and set(voices.values()) <= {v.id for v in ORPHEUS_VOICES}


def test_guests_get_voices_nobody_else_uses() -> None:
    # Three main characters leave Orpheus exactly two free voices: one for each allowed guest.
    main = assign_voices("orpheus", ["pip", "olly", "mama_owl"], "s")
    voices = with_guest_voices("orpheus", main, ["owl", "fish"], "s")
    assert {k: voices[k] for k in main} == main  # main characters keep their voices
    assert len(set(voices.values())) == 6  # narrator, 3 characters, 2 guests: all different
    assert voices == with_guest_voices("orpheus", main, ["fish", "owl"], "s")  # stable on every page


def test_guests_reuse_voices_once_the_pool_is_used_up() -> None:
    main = assign_voices("orpheus", ["a", "b", "c", "d", "e"], "s")
    voices = with_guest_voices("orpheus", main, ["owl"], "s")
    assert voices["owl"] in {v.id for v in ORPHEUS_VOICES}


def test_voice_lookup() -> None:
    assert voice("kokoro", "bf_emma").accent == "en-gb"
    assert {v.id for v in ORPHEUS_VOICES} == {"autumn", "diana", "hannah", "austin", "daniel", "troy"}
    assert len({v.id for v in KOKORO_VOICES}) == len(KOKORO_VOICES)
    with pytest.raises(KeyError):
        voice("orpheus", "af_heart")
