"""Fake TTS engines and helpers shared by the text-to-speech unit tests."""

import numpy as np

from storyweaver.routing.router import ModelRouter
from storyweaver.routing.store import InMemoryQuotaStore
from storyweaver.routing.tts import TTSMode, TTSRouter
from storyweaver.speech.tts import TTS_SAMPLE_RATE, EngineName, Speech

ORPHEUS = "canopylabs/orpheus-v1-english"
SECONDS_PER_CHAR = 0.05


class FakeTTS:
    """Returns a tone whose length depends on the text; records every call."""

    def __init__(self, name: EngineName, *, errors: list[Exception] | None = None) -> None:
        self.name: EngineName = name
        self.errors = list(errors or [])
        self.calls: list[tuple[str, str]] = []

    async def synthesize(self, text: str, voice: str) -> Speech:
        self.calls.append((text, voice))
        if self.errors:
            raise self.errors.pop(0)
        n = int(len(text) * SECONDS_PER_CHAR * TTS_SAMPLE_RATE)
        samples = (0.2 * np.sin(np.arange(n) * 0.1)).astype(np.float32)
        return Speech(samples=samples, sample_rate=TTS_SAMPLE_RATE)


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


class FakeSleep:
    """Advances the router's clock instead of sleeping."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.clock.now += seconds


def make_router(
    orpheus: FakeTTS | None,
    kokoro: FakeTTS | None,
    *,
    mode: TTSMode = "auto",
    max_wait: float = 120.0,
    clock: Clock | None = None,
) -> tuple[TTSRouter, FakeSleep]:
    clock = clock or Clock()
    sleep = FakeSleep(clock)
    router = TTSRouter(
        router=ModelRouter(InMemoryQuotaStore(), margin=0.9, clock=clock),
        orpheus=orpheus,
        orpheus_model=ORPHEUS,
        kokoro=kokoro,
        mode=mode,
        max_wait_seconds=max_wait,
        sleep=sleep,
    )
    return router, sleep
