"""Opt-in live speech-to-text tests (plan §12: one live test per model).

The fixture is a synthetic voice (Windows speech synthesis), not a real person, saying
"A little dragon who is scared of the dark."

Run:
  LIVE_GROQ_API_KEY=... uv run pytest -m live tests/live/test_stt_live.py -s     # Groq Whisper (1 request)
  LIVE_LOCAL_STT=1 uv run pytest -m live tests/live/test_stt_live.py -s   # faster-whisper, ~460 MB download
"""

import os
from pathlib import Path

import pytest
from groq import AsyncGroq

from storyweaver.adapters.faster_whisper import FasterWhisper
from storyweaver.adapters.groq_whisper import GroqWhisper
from storyweaver.speech.audio_utils import decode_to_mono_16k, is_silent

pytestmark = pytest.mark.live

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "speech_dragon.wav"
KEY_WORDS = ("dragon", "scared", "dark")


def _words(text: str) -> str:
    return text.lower()


@pytest.mark.skipif(not os.getenv("LIVE_GROQ_API_KEY"), reason="LIVE_GROQ_API_KEY not set")
async def test_groq_whisper_transcribes_the_fixture() -> None:
    audio = decode_to_mono_16k(FIXTURE.read_bytes())
    assert not is_silent(audio)
    client = AsyncGroq(api_key=os.environ["LIVE_GROQ_API_KEY"], max_retries=0, timeout=60.0)
    result = await GroqWhisper(client, model="whisper-large-v3-turbo").transcribe(audio)
    await client.close()
    print(f"\nGroq Whisper ({result.latency_ms:.0f} ms): {result.text!r}")
    assert all(word in _words(result.text) for word in KEY_WORDS)


@pytest.mark.skipif(not os.getenv("LIVE_LOCAL_STT"), reason="LIVE_LOCAL_STT not set (downloads ~460 MB)")
async def test_faster_whisper_transcribes_the_fixture() -> None:
    audio = decode_to_mono_16k(FIXTURE.read_bytes())
    result = await FasterWhisper(model_size="small").transcribe(audio)
    print(f"\nfaster-whisper small ({result.latency_ms:.0f} ms after load): {result.text!r}")
    assert all(word in _words(result.text) for word in KEY_WORDS)
