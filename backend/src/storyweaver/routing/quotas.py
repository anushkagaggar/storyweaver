"""Groq free-tier limits per model (plan §2.1, §6.3).

Source of truth: docs/groq-limits.md (docs table, confirmed against this account's rate-limit headers on
14 Oct 2026). Limits apply per organisation, so every API key shares them. Update both places together.
"""

from dataclasses import dataclass
from typing import Literal

Unit = Literal["requests", "tokens", "audio_seconds"]

MINUTE = 60
HOUR = 3600
DAY = 86_400


@dataclass(frozen=True)
class Limit:
    unit: Unit
    window_seconds: int
    amount: int

    @property
    def name(self) -> str:
        return f"{self.unit}/{self.window_seconds}s"


@dataclass(frozen=True)
class ModelLimits:
    rpm: int | None = None
    rpd: int | None = None
    tpm: int | None = None
    tpd: int | None = None
    audio_seconds_per_hour: int | None = None
    audio_seconds_per_day: int | None = None

    def limits(self, margin: float = 1.0) -> list[Limit]:
        """Limits scaled by `margin` (e.g. 0.9 keeps 10% headroom below Groq's numbers)."""
        pairs: list[tuple[Unit, int, int | None]] = [
            ("requests", MINUTE, self.rpm),
            ("requests", DAY, self.rpd),
            ("tokens", MINUTE, self.tpm),
            ("tokens", DAY, self.tpd),
            ("audio_seconds", HOUR, self.audio_seconds_per_hour),
            ("audio_seconds", DAY, self.audio_seconds_per_day),
        ]
        return [
            Limit(unit, window, max(1, int(amount * margin)))
            for unit, window, amount in pairs
            if amount is not None
        ]


GROQ_FREE_TIER: dict[str, ModelLimits] = {
    "openai/gpt-oss-20b": ModelLimits(rpm=30, rpd=1_000, tpm=8_000, tpd=200_000),
    "openai/gpt-oss-120b": ModelLimits(rpm=30, rpd=1_000, tpm=8_000, tpd=200_000),
    "qwen/qwen3.8-27b": ModelLimits(rpm=30, rpd=1_000, tpm=8_000, tpd=200_000),
    "whisper-large-v3-turbo": ModelLimits(
        rpm=20, rpd=2_000, audio_seconds_per_hour=7_200, audio_seconds_per_day=28_800
    ),
    "canopylabs/orpheus-v1-english": ModelLimits(rpm=10, rpd=100, tpm=1_200, tpd=3_600),
    "meta-llama/llama-prompt-guard-2-22m": ModelLimits(rpm=30, rpd=14_400, tpm=15_000, tpd=500_000),
    "openai/gpt-oss-safeguard-20b": ModelLimits(rpm=3, rpd=1_000, tpm=2_000, tpd=200_000),
}


def limits_for(model: str) -> ModelLimits:
    try:
        return GROQ_FREE_TIER[model]
    except KeyError:
        # An unknown model must not silently run without limits.
        raise KeyError(f"No Groq limits configured for model {model!r}; add it to quotas.py.") from None
