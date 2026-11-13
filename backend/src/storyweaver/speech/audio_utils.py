"""Audio intake for speech-to-text (plan V2): decode, normalise to 16 kHz mono, and reject bad recordings.

Privacy (plan §6.6): audio is only ever held in memory. Decoding uses PyAV (FFmpeg's libraries as a Python
wheel) on an in-memory buffer, so no ffmpeg subprocess, temp file or system package is involved, and the
same code runs on Windows, CI, Docker and the Space. Nothing in this module logs audio or transcripts.
"""

import io
import wave
from dataclasses import dataclass

import av
import numpy as np
from av.audio.resampler import AudioResampler
from numpy.typing import NDArray

SAMPLE_RATE = 16_000
MAX_AUDIO_BYTES = 10 * 1024 * 1024
MAX_AUDIO_SECONDS = 60
# MediaRecorder stops a fraction of a second late, so a "60 s" recording is accepted up to this.
DURATION_TOLERANCE_SECONDS = 0.5

# Containers browsers record (Chrome/Firefox: webm or ogg with Opus; Safari: mp4/AAC) plus common uploads.
ALLOWED_AUDIO_TYPES = frozenset(
    {
        "audio/webm",
        "audio/ogg",
        "audio/mp4",
        "audio/m4a",
        "audio/x-m4a",
        "audio/aac",
        "audio/mpeg",
        "audio/mp3",
        "audio/wav",
        "audio/x-wav",
        "audio/wave",
        "audio/flac",
        "audio/x-flac",
    }
)

# Silence check: 30 ms frames; a frame is "voiced" above this loudness. A real mic's noise floor sits around
# -60 to -50 dBFS and speech around -30 to -15 dBFS, so -40 dBFS separates them with room either side.
FRAME_SECONDS = 0.03
VOICED_FRAME_DBFS = -40.0
MIN_VOICED_SECONDS = 0.25


class AudioError(Exception):
    """The recording can't be transcribed. `code` is returned to the client; messages never contain audio."""

    code = "audio_invalid"


class AudioDecodeError(AudioError):
    code = "audio_unreadable"


class AudioTooLongError(AudioError):
    code = "audio_too_long"


@dataclass(frozen=True)
class NormalizedAudio:
    """16 kHz mono float32 samples in [-1, 1], the input every STT engine takes."""

    samples: NDArray[np.float32]
    sample_rate: int = SAMPLE_RATE

    @property
    def duration_seconds(self) -> float:
        return len(self.samples) / self.sample_rate


def media_type(content_type: str | None) -> str:
    """'audio/webm;codecs=opus' -> 'audio/webm'."""
    return (content_type or "").split(";", 1)[0].strip().lower()


def decode_to_mono_16k(data: bytes, *, max_seconds: float = MAX_AUDIO_SECONDS) -> NormalizedAudio:
    """Decode any supported container in memory and resample to 16 kHz mono.

    Stops decoding as soon as the audio passes `max_seconds` (plus tolerance), so a small, highly compressed
    file can't expand into minutes of samples in memory.
    """
    return NormalizedAudio(samples=decode_mono(data, rate=SAMPLE_RATE, max_seconds=max_seconds))


def decode_mono(data: bytes, *, rate: int, max_seconds: float) -> NDArray[np.float32]:
    """Decode in memory to mono float32 samples at `rate`. Raises AudioDecodeError / AudioTooLongError."""
    if not data:
        raise AudioDecodeError("The recording is empty.")
    limit = int((max_seconds + DURATION_TOLERANCE_SECONDS) * rate)
    chunks: list[NDArray[np.int16]] = []
    total = 0
    try:
        with av.open(io.BytesIO(data), mode="r") as container:
            if not container.streams.audio:
                raise AudioDecodeError("The file has no audio track.")
            stream = container.streams.audio[0]
            resampler = AudioResampler(format="s16", layout="mono", rate=rate)
            for frame in container.decode(stream):
                for out in resampler.resample(frame):
                    total += _append(chunks, out)
                if total > limit:
                    raise AudioTooLongError(f"The recording is longer than {max_seconds:.0f} seconds.")
            for out in resampler.resample(None):  # flush
                total += _append(chunks, out)
    except AudioError:
        raise
    except Exception as exc:  # PyAV/FFmpeg raise many types for corrupt or unsupported input
        raise AudioDecodeError("The recording could not be read.") from exc
    if total > limit:
        raise AudioTooLongError(f"The recording is longer than {max_seconds:.0f} seconds.")
    if total == 0:
        raise AudioDecodeError("The recording has no sound data.")
    pcm = np.concatenate(chunks)
    return pcm.astype(np.float32) / 32768.0


def _append(chunks: list[NDArray[np.int16]], frame: av.AudioFrame) -> int:
    samples = frame.to_ndarray().reshape(-1).astype(np.int16, copy=False)
    chunks.append(samples)
    return len(samples)


def voiced_seconds(audio: NormalizedAudio) -> float:
    """Seconds of 30 ms frames louder than VOICED_FRAME_DBFS."""
    frame = int(FRAME_SECONDS * audio.sample_rate)
    usable = len(audio.samples) // frame * frame
    if usable == 0:
        return 0.0
    frames = audio.samples[:usable].reshape(-1, frame).astype(np.float64)
    rms = np.sqrt(np.mean(frames**2, axis=1))
    dbfs = 20 * np.log10(np.maximum(rms, 1e-10))
    return float(np.count_nonzero(dbfs > VOICED_FRAME_DBFS) * FRAME_SECONDS)


def is_silent(audio: NormalizedAudio) -> bool:
    return voiced_seconds(audio) < MIN_VOICED_SECONDS


def to_wav_bytes(audio: NormalizedAudio) -> bytes:
    """16-bit PCM WAV in memory (what we send to Groq: small, lossless and universally accepted)."""
    pcm = (np.clip(audio.samples, -1.0, 1.0) * 32767).astype("<i2")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(audio.sample_rate)
        wav.writeframes(pcm.tobytes())
    return buffer.getvalue()


def encode_mp3(samples: NDArray[np.float32], rate: int, *, bitrate: int = 64_000) -> bytes:
    """Mono MP3 in memory (plays everywhere, including Safari; about 8 KB per second at 64 kbps)."""
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
    buffer = io.BytesIO()
    with av.open(buffer, mode="w", format="mp3") as container:
        stream = container.add_stream("libmp3lame", rate=rate, layout="mono")
        stream.bit_rate = bitrate
        frame = av.AudioFrame.from_ndarray(pcm.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = rate
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    return buffer.getvalue()
