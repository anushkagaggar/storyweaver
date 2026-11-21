"""Quota-aware model routing (plan §6.3): reserve quota before every Groq call, fall back when it is short."""

import logging
import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from storyweaver.routing.quotas import DAY, Limit, limits_for
from storyweaver.routing.store import Blocked, QuotaStore, Reservation, Usage

logger = logging.getLogger(__name__)

# A wait this long can only come from a daily window (per-minute limits and 429 cool-downs clear within a
# minute or two), so the answer is "for today", not "in a moment".
DAILY_WAIT_SECONDS = 3600


def is_daily_wait(seconds: float | None) -> bool:
    return seconds is not None and seconds >= DAILY_WAIT_SECONDS


@dataclass
class QuotaExhaustedError(Exception):
    """No candidate model has quota right now. Retry after `retry_after_seconds`."""

    retry_after_seconds: float
    reasons: list[str] = field(default_factory=list)

    @property
    def daily(self) -> bool:
        """Used up for today (a daily window), rather than briefly rate limited."""
        return is_daily_wait(self.retry_after_seconds)

    def __str__(self) -> str:
        return f"Quota exhausted; retry after {self.retry_after_seconds:.0f}s ({'; '.join(self.reasons)})"


class ModelRouter:
    def __init__(
        self, store: QuotaStore, *, margin: float = 0.9, clock: Callable[[], float] = time.time
    ) -> None:
        self._store = store
        self._margin = margin
        self._clock = clock

    async def acquire(
        self, candidates: Sequence[str], usage: Usage, *, windows: Callable[[Limit], bool] | None = None
    ) -> Reservation:
        """Reserve `usage` on the first candidate with quota, in order. Raises QuotaExhaustedError.

        `windows` restricts which limits are checked and charged, e.g. only the daily ones (`daily_limits`)
        to book a whole story's budget up front, then only the per-minute ones while pacing its requests.
        """
        now = self._clock()
        waits: list[float] = []
        reasons: list[str] = []
        for model in candidates:
            cooldown = await self._store.cooldown_remaining(model, now)
            if cooldown > 0:
                waits.append(cooldown)
                reasons.append(f"{model}: cooling down after a 429")
                continue
            limits = [
                limit for limit in limits_for(model).limits(self._margin) if windows is None or windows(limit)
            ]
            result = await self._store.reserve(model, limits, usage, now)
            if isinstance(result, Reservation):
                return result
            blocked: Blocked = result
            waits.append(blocked.retry_after_seconds)
            reasons.append(f"{model}: {blocked.limit.name} limit {blocked.limit.amount} reached")
        retry_after = math.ceil(min(waits)) if waits else 60
        logger.warning("Quota exhausted for %s: %s", list(candidates), reasons)
        raise QuotaExhaustedError(retry_after_seconds=retry_after, reasons=reasons)

    async def settle(self, reservation: Reservation, actual_tokens: int) -> None:
        """Replace the reserved token estimate with the tokens Groq actually counted."""
        await self._store.adjust(reservation, "tokens", actual_tokens - reservation.usage.tokens)

    async def release_tokens(self, reservation: Reservation) -> None:
        """The call failed before generating output: return the reserved tokens (the request still counts)."""
        await self._store.adjust(reservation, "tokens", -reservation.usage.tokens)

    async def cooldown(self, model: str, seconds: float) -> None:
        await self._store.set_cooldown(model, seconds, self._clock())


def daily_limits(limit: Limit) -> bool:
    return limit.window_seconds >= DAY


def short_window_limits(limit: Limit) -> bool:
    return limit.window_seconds < DAY
