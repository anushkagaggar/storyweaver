import io
import wave

import numpy as np
import pytest

from audio_samples import hiss, silence, tone
from storyweaver.speech.audio_utils import (
    SAMPLE_RATE,
    AudioDecodeError,
    AudioTooLongError,
    NormalizedAudio,
    decode_to_mono_16k,
    is_silent,
    media_type,
    to_wav_bytes,
    voiced_seconds,
)

pytestmark = pytest.mark.unit


def test_stereo_44k_becomes_16k_mono_with_the_same_duration() -> None:
    audio = decode_to_mono_16k(tone(2.0, rate=44_100, channels=2))
    assert audio.sample_rate == SAMPLE_RATE
    assert audio.samples.ndim == 1
    assert audio.samples.dtype == np.float32
    assert audio.duration_seconds == pytest.approx(2.0, abs=0.05)
    assert float(np.max(np.abs(audio.samples))) <= 1.0


def test_exactly_sixty_seconds_is_accepted() -> None:
    assert decode_to_mono_16k(tone(60.0, rate=16_000, channels=1)).duration_seconds == pytest.approx(
        60, abs=0.1
    )


def test_over_sixty_seconds_is_rejected() -> None:
    with pytest.raises(AudioTooLongError):
        decode_to_mono_16k(tone(62.0, rate=16_000, channels=1))


def test_two_minutes_stop_decoding_early() -> None:
    with pytest.raises(AudioTooLongError):
        decode_to_mono_16k(tone(120.0, rate=8_000, channels=1))


@pytest.mark.parametrize("data", [b"", b"not audio at all", b"RIFF\x00\x00\x00\x00WAVEjunk", bytes(4096)])
def test_empty_or_garbage_is_unreadable(data: bytes) -> None:
    with pytest.raises(AudioDecodeError):
        decode_to_mono_16k(data)


def test_errors_never_carry_the_audio() -> None:
    secret = b"PRIVATE-AUDIO-BYTES" * 10
    with pytest.raises(AudioDecodeError) as caught:
        decode_to_mono_16k(secret)
    assert "PRIVATE" not in str(caught.value)


def test_silence_and_hiss_are_silent_but_a_tone_is_not() -> None:
    assert is_silent(decode_to_mono_16k(silence(3.0)))
    assert is_silent(decode_to_mono_16k(hiss(3.0)))
    loud = decode_to_mono_16k(tone(1.0))
    assert not is_silent(loud)
    assert voiced_seconds(loud) == pytest.approx(1.0, abs=0.06)


def test_a_click_is_too_short_to_count_as_speech() -> None:
    assert is_silent(decode_to_mono_16k(tone(0.1)))


def test_wav_export_is_16k_mono_pcm() -> None:
    audio = NormalizedAudio(samples=np.zeros(SAMPLE_RATE, dtype=np.float32))
    with wave.open(io.BytesIO(to_wav_bytes(audio))) as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getnframes()) == (
            1,
            2,
            16_000,
            16_000,
        )


@pytest.mark.parametrize(
    ("header", "expected"),
    [("audio/webm;codecs=opus", "audio/webm"), ("Audio/MP4", "audio/mp4"), (None, ""), ("", "")],
)
def test_media_type(header: str | None, expected: str) -> None:
    assert media_type(header) == expected


def test_real_speech_passes_the_silence_check() -> None:
    from pathlib import Path

    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "speech_dragon.wav"
    audio = decode_to_mono_16k(fixture.read_bytes())
    assert 1.5 < audio.duration_seconds < 6
    assert not is_silent(audio)
