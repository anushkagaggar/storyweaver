"""Kokoro-82M implementation of TTSEngine via kokoro-onnx (CPU, no torch).

Weights: hexgrad/Kokoro-82M, Apache-2.0, as ONNX files from the kokoro-onnx v1.1 model release. They are
downloaded on first use (not at startup, so cold starts stay fast), checked against pinned SHA-256
digests, and cached in KOKORO_DIR. Synthesis runs in a thread, one call at a time, so the event loop and
the Space's CPU aren't swamped.
"""

import asyncio
import hashlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import numpy as np

from storyweaver.speech.tts import TTS_SAMPLE_RATE, EngineName, Speech, TTSError, TTSUnavailableError
from storyweaver.speech.voices import KOKORO_VOICES, voice

logger = logging.getLogger(__name__)

RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1"


@dataclass(frozen=True)
class ModelFile:
    name: str
    sha256: str


# Digests published with the GitHub release (checked 13 Nov 2026).
# These are public file checksums, not secrets (hence the allowlist pragmas for detect-secrets).
MODEL_FILES: dict[str, ModelFile] = {
    "int8": ModelFile(
        "kokoro-v1.0.int8.onnx",
        "ae315a79b623f244700e4afb9246c46a26066782e049ba174bf3ba433970ee9c",  # pragma: allowlist secret
    ),
    "fp16": ModelFile(
        "kokoro-v1.0.fp16.onnx",
        "f3a290d384fbb27966d462905c71a46cef9e5fd00516b40df32a0b4afe77ac96",  # pragma: allowlist secret
    ),
    "fp32": ModelFile(
        "kokoro-v1.0.onnx",
        "beb0d1848dee9a49da392cc3df26958d46cfa35d321edf434f52949153f0df3a",  # pragma: allowlist secret
    ),
}
VOICES_FILE = ModelFile(
    "voices-v1.0.bin",
    "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d",  # pragma: allowlist secret
)

ModelFactory = Callable[[], Any]


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def ensure_file(directory: Path, spec: ModelFile, *, base_url: str = RELEASE) -> Path:
    """Download `spec` into `directory` once, verifying its SHA-256. A bad download is deleted."""
    target = directory / spec.name
    if target.exists() and sha256_of(target) == spec.sha256:
        return target
    directory.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".part")
    logger.info("Downloading %s ...", spec.name)
    with httpx.stream("GET", f"{base_url}/{spec.name}", follow_redirects=True, timeout=120.0) as response:
        response.raise_for_status()
        with partial.open("wb") as handle:
            for block in response.iter_bytes(1 << 20):
                handle.write(block)
    if sha256_of(partial) != spec.sha256:
        partial.unlink(missing_ok=True)
        raise TTSUnavailableError(f"{spec.name} failed its SHA-256 check.")
    partial.replace(target)
    return target


class Kokoro:
    name: EngineName = "kokoro"

    def __init__(
        self, *, directory: Path, variant: str = "fp32", model_factory: ModelFactory | None = None
    ) -> None:
        if variant not in MODEL_FILES:
            raise ValueError(f"KOKORO_VARIANT must be one of {sorted(MODEL_FILES)}, got {variant!r}")
        self.model = f"kokoro-82m-{variant}"
        self._directory = directory
        self._variant = variant
        self._factory = model_factory or self._load_kokoro
        self._loaded: Any | None = None
        self._lock = asyncio.Lock()

    def _load_kokoro(self) -> Any:
        try:
            from kokoro_onnx import Kokoro as KokoroOnnx
        except ImportError as exc:
            raise TTSUnavailableError("kokoro-onnx is not installed.") from exc
        model = ensure_file(self._directory, MODEL_FILES[self._variant])
        voices = ensure_file(self._directory, VOICES_FILE)
        engine = KokoroOnnx(str(model), str(voices))
        missing = {v.id for v in KOKORO_VOICES} - set(engine.get_voices())
        if missing:
            raise TTSUnavailableError(f"Kokoro voices file lacks {sorted(missing)}.")
        return engine

    async def _engine(self) -> Any:
        if self._loaded is None:
            async with self._lock:
                if self._loaded is None:
                    started = time.perf_counter()
                    try:
                        self._loaded = await asyncio.to_thread(self._factory)
                    except TTSError:
                        raise
                    except Exception as exc:  # download or load failure
                        raise TTSUnavailableError(
                            f"Could not load {self.model}: {type(exc).__name__}"
                        ) from exc
                    logger.info("Loaded %s in %.1f s.", self.model, time.perf_counter() - started)
        return self._loaded

    async def synthesize(self, text: str, voice_id: str) -> Speech:
        accent = voice("kokoro", voice_id).accent  # KeyError for an unknown voice: a programming error
        engine = await self._engine()
        async with self._lock:  # one synthesis at a time on the shared CPU
            try:
                samples, rate = await asyncio.to_thread(
                    engine.create, text, voice=voice_id, speed=1.0, lang=accent
                )
            except Exception as exc:
                raise TTSUnavailableError(f"{self.model} failed: {type(exc).__name__}") from exc
        if rate != TTS_SAMPLE_RATE:
            raise TTSUnavailableError(f"{self.model} returned {rate} Hz audio, expected {TTS_SAMPLE_RATE}.")
        return Speech(samples=np.asarray(samples, dtype=np.float32).reshape(-1), sample_rate=TTS_SAMPLE_RATE)
