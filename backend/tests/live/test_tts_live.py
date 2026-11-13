"""Opt-in live text-to-speech tests (plan §12: one live test per model). Spoken back through Whisper to
check the words, so each test also proves the audio is real speech.

Run:
  LIVE_GROQ_API_KEY=... uv run pytest -m live tests/live/test_tts_live.py -s   # Orpheus (~40 chars of quota)
  LIVE_LOCAL_TTS=1 KOKORO_DIR=D:/lenovo/models/kokoro uv run pytest -m live tests/live/test_tts_live.py -s
      (Kokoro: downloads ~350 MB once; transcribed with Groq Whisper if LIVE_GROQ_API_KEY is set)
"""

import os
from pathlib import Path

import pytest
from groq import AsyncGroq

from storyweaver.adapters.groq_orpheus import GroqOrpheus
from storyweaver.adapters.groq_whisper import GroqWhisper
from storyweaver.adapters.kokoro_tts import Kokoro
from storyweaver.speech.audio_utils import NormalizedAudio, decode_mono, encode_mp3, is_silent
from storyweaver.speech.tts import TTS_SAMPLE_RATE, Speech

pytestmark = pytest.mark.live

TEXT = "The sleepy dragon said goodnight to the moon."


async def _heard(speech: Speech) -> str | None:
    """Whisper's transcript of `speech`, or None without a Groq key."""
    if not os.getenv("LIVE_GROQ_API_KEY"):
        return None
    audio = NormalizedAudio(
        samples=decode_mono(encode_mp3(speech.samples, speech.sample_rate), rate=16_000, max_seconds=60)
    )
    client = AsyncGroq(api_key=os.environ["LIVE_GROQ_API_KEY"], max_retries=0, timeout=60.0)
    try:
        return (await GroqWhisper(client, model="whisper-large-v3-turbo").transcribe(audio)).text.lower()
    finally:
        await client.close()


def _check(speech: Speech) -> None:
    assert speech.sample_rate == TTS_SAMPLE_RATE
    assert 1.5 < speech.duration_seconds < 10
    assert not is_silent(NormalizedAudio(samples=speech.samples, sample_rate=speech.sample_rate))


@pytest.mark.skipif(not os.getenv("LIVE_GROQ_API_KEY"), reason="LIVE_GROQ_API_KEY not set")
async def test_orpheus_speaks() -> None:
    client = AsyncGroq(api_key=os.environ["LIVE_GROQ_API_KEY"], max_retries=0, timeout=60.0)
    try:
        speech = await GroqOrpheus(client, model="canopylabs/orpheus-v1-english").synthesize(TEXT, "hannah")
    finally:
        await client.close()
    _check(speech)
    heard = await _heard(speech)
    print(f"\nOrpheus: {speech.duration_seconds:.1f} s; Whisper heard {heard!r}")
    assert heard is not None and "dragon" in heard and "moon" in heard


@pytest.mark.skipif(not os.getenv("LIVE_LOCAL_TTS"), reason="LIVE_LOCAL_TTS not set (downloads ~350 MB)")
@pytest.mark.parametrize("voice", ["af_heart", "bm_george"])
async def test_kokoro_speaks(voice: str) -> None:
    directory = Path(os.getenv("KOKORO_DIR", str(Path.home() / ".cache" / "storyweaver" / "kokoro")))
    speech = await Kokoro(directory=directory).synthesize(TEXT, voice)
    _check(speech)
    heard = await _heard(speech)
    print(f"\nKokoro {voice}: {speech.duration_seconds:.1f} s; Whisper heard {heard!r}")
    if heard is not None:
        assert "dragon" in heard and "moon" in heard
