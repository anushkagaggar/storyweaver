from typing import Any

import pytest

from storyweaver.domain.story import NARRATOR
from storyweaver.llm.schemas import PlannedStory
from storyweaver.orchestrator.narration import (
    PAUSE_BETWEEN_CHUNKS,
    PAUSE_BETWEEN_LINES,
    Narrator,
    SpokenLine,
    scene_lines,
)
from storyweaver.routing.router import QuotaExhaustedError
from storyweaver.speech.audio_utils import decode_mono
from storyweaver.speech.tts import TTS_SAMPLE_RATE, TTSRateLimitedError, TTSUnavailableError
from storyweaver.speech.voices import assign_voices
from tts_fakes import SECONDS_PER_CHAR, FakeTTS, make_router

pytestmark = pytest.mark.unit


async def test_lines_are_spoken_in_order_with_pauses_and_encoded_as_mp3() -> None:
    kokoro = FakeTTS("kokoro")
    router, _ = make_router(None, kokoro)
    lines = [SpokenLine("af_heart", "Once upon a time."), SpokenLine("am_puck", "Hello, owl!")]
    audio = await Narrator(router).narrate(lines, "kokoro")

    assert kokoro.calls == [("Once upon a time.", "af_heart"), ("Hello, owl!", "am_puck")]
    expected = (len("Once upon a time.") + len("Hello, owl!")) * SECONDS_PER_CHAR + PAUSE_BETWEEN_LINES
    assert audio.duration_seconds == pytest.approx(expected, abs=0.01)
    assert audio.engine == "kokoro"
    assert audio.mp3[:3] == b"ID3" or audio.mp3[:2] == b"\xff\xfb"
    decoded = decode_mono(audio.mp3, rate=TTS_SAMPLE_RATE, max_seconds=60)
    assert len(decoded) / TTS_SAMPLE_RATE == pytest.approx(expected, abs=0.1)


async def test_long_lines_are_chunked_with_short_pauses() -> None:
    kokoro = FakeTTS("kokoro")
    router, _ = make_router(None, kokoro)
    text = " ".join(["The fox ran far across the wide green meadow."] * 6)  # about 280 characters
    audio = await Narrator(router).narrate([SpokenLine("af_heart", text)], "kokoro")
    assert len(kokoro.calls) == 2
    spoken = sum(len(chunk) for chunk, _ in kokoro.calls) * SECONDS_PER_CHAR
    assert audio.duration_seconds == pytest.approx(spoken + PAUSE_BETWEEN_CHUNKS, abs=0.01)


def test_scene_lines_use_each_speakers_voice_and_unknown_speakers_get_the_narrator(
    planned_story: dict[str, Any],
) -> None:
    story = PlannedStory.model_validate(planned_story).to_story(age_band="4-6", story_key="s")
    scene = story.scenes[0]
    voices = assign_voices("kokoro", [c.id for c in story.characters], "s")
    lines = scene_lines(scene.lines, voices)
    assert [line.text for line in lines] == [line.text for line in scene.lines]
    for spoken, line in zip(lines, scene.lines, strict=True):
        assert spoken.voice == voices[line.speaker]

    only_narrator = {NARRATOR: voices[NARRATOR]}
    assert {line.voice for line in scene_lines(scene.lines, only_narrator)} == {voices[NARRATOR]}


async def test_narrate_text_uses_orpheus_when_it_fits() -> None:
    orpheus, kokoro = FakeTTS("orpheus"), FakeTTS("kokoro")
    router, _ = make_router(orpheus, kokoro)
    audio, voice_id = await Narrator(router).narrate_text("Goodnight, little fox.")
    assert (audio.engine, voice_id) == ("orpheus", "hannah")
    assert kokoro.calls == []


@pytest.mark.parametrize("error", [TTSRateLimitedError(20), TTSUnavailableError("down")])
async def test_narrate_text_falls_back_to_kokoro_when_orpheus_fails(error: Exception) -> None:
    orpheus, kokoro = FakeTTS("orpheus", errors=[error]), FakeTTS("kokoro")
    router, _ = make_router(orpheus, kokoro)
    audio, voice_id = await Narrator(router).narrate_text("Goodnight.")
    assert (audio.engine, voice_id) == ("kokoro", "af_heart")


async def test_narrate_text_with_a_kokoro_voice_skips_orpheus() -> None:
    orpheus, kokoro = FakeTTS("orpheus"), FakeTTS("kokoro")
    router, _ = make_router(orpheus, kokoro)
    audio, voice_id = await Narrator(router).narrate_text("Hi.", "bm_george")
    assert (audio.engine, voice_id) == ("kokoro", "bm_george")
    assert orpheus.calls == []


async def test_narrate_text_with_an_orpheus_voice_needs_orpheus_quota() -> None:
    router, _ = make_router(FakeTTS("orpheus"), FakeTTS("kokoro"))
    narrator = Narrator(router)
    audio, voice_id = await narrator.narrate_text("Hi.", "troy")
    assert (audio.engine, voice_id) == ("orpheus", "troy")
    assert await router.choose(["word " * 640]) == "orpheus"  # books 3,199 of the 3,240 characters a day
    with pytest.raises(QuotaExhaustedError):
        await narrator.narrate_text(
            "A hundred characters of story text that no longer fit in today's budget at all.", "troy"
        )


async def test_narrate_text_with_an_unknown_voice_is_a_key_error() -> None:
    router, _ = make_router(FakeTTS("orpheus"), FakeTTS("kokoro"))
    with pytest.raises(KeyError):
        await Narrator(router).narrate_text("Hi.", "nobody")
