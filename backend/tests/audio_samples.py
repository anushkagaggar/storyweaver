"""Synthetic recordings for speech tests, built in memory (no files, no real voices)."""

import io
import wave

import numpy as np


def wav_bytes(samples: np.ndarray, *, rate: int, channels: int = 1) -> bytes:
    """16-bit PCM WAV. `samples` is float in [-1, 1], shape (n,) or (n, channels)."""
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm.tobytes())
    return buffer.getvalue()


def tone(seconds: float, *, rate: int = 44_100, channels: int = 2, amplitude: float = 0.3) -> bytes:
    """A 440 Hz tone: loud enough to count as 'voiced' for the silence check."""
    t = np.arange(int(seconds * rate)) / rate
    mono = amplitude * np.sin(2 * np.pi * 440 * t)
    samples = np.repeat(mono[:, None], channels, axis=1) if channels > 1 else mono
    return wav_bytes(samples, rate=rate, channels=channels)


def silence(seconds: float, *, rate: int = 16_000) -> bytes:
    return wav_bytes(np.zeros(int(seconds * rate)), rate=rate)


def hiss(seconds: float, *, rate: int = 16_000, amplitude: float = 0.002) -> bytes:
    """Quiet background noise (about -60 dBFS), like a mic in a quiet room."""
    rng = np.random.default_rng(7)
    return wav_bytes(rng.normal(0, amplitude, int(seconds * rate)), rate=rate)
