"""Our own limits, on top of the providers' (plan V3): per-visitor story creation and a daily GPU budget.

Every visitor's GPU work is charged to one Hugging Face account (ZeroGPU quota) and every story to one Groq
organisation, so one visitor must not be able to use up the day for everyone:

- `VisitorLimiter`: stories per visitor per hour and per day. There are no accounts, so a visitor is a
  client IP, forwarded by the Vercel route handler in `X-Client-IP`. Only that server holds the API key, so
  the header can be trusted. The IP is never stored: counters are keyed by an HMAC of it (keyed with the
  API key), so they can't be reversed by hashing all 2^32 IPv4 addresses, and they expire within a day.
- `GpuBudget`: GPU-seconds per day across everyone, kept below the account's ZeroGPU allowance. When it
  runs out, pages get the placeholder picture instead of failing.

Both reuse the sliding-window counters of the Groq router (routing/store.py), in Redis.
"""

import hashlib
import hmac
import time
from collections.abc import Callable
from dataclasses import dataclass

from storyweaver.routing.quotas import DAY, HOUR, Limit
from storyweaver.routing.store import Blocked, QuotaStore, Reservation, Usage

UNIDENTIFIED = "unidentified"  # callers that send no X-Client-IP (MCP server, scripts) share one bucket


@dataclass
class LimitExceededError(Exception):
    retry_after_seconds: float
    window: str  # "hour" or "day"

    def __str__(self) -> str:
        return f"Limit per {self.window} reached; retry after {self.retry_after_seconds:.0f}s"


def _window(limit: Limit) -> str:
    return "day" if limit.window_seconds >= DAY else "hour"


class VisitorLimiter:
    def __init__(
        self,
        store: QuotaStore,
        *,
        per_hour: int,
        per_day: int,
        secret: str,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._limits = [Limit("requests", HOUR, per_hour), Limit("requests", DAY, per_day)]
        self._secret = secret.encode()
        self._clock = clock

    def visitor_key(self, client_ip: str | None) -> str:
        ip = (client_ip or "").strip()
        if not ip:
            return UNIDENTIFIED
        return hmac.new(self._secret, ip.encode(), hashlib.sha256).hexdigest()[:32]

    async def acquire(self, client_ip: str | None) -> None:
        """Count one story for this visitor, or raise LimitExceededError."""
        result = await self._store.reserve(
            f"visitor:{self.visitor_key(client_ip)}", self._limits, Usage(requests=1), self._clock()
        )
        if isinstance(result, Blocked):
            raise LimitExceededError(result.retry_after_seconds, _window(result.limit))


class GpuBudget:
    def __init__(
        self, store: QuotaStore, *, seconds_per_day: int, clock: Callable[[], float] = time.time
    ) -> None:
        self._store = store
        self._limits = [Limit("gpu_seconds", DAY, seconds_per_day)]
        self._clock = clock

    async def reserve(self, estimate_seconds: float) -> Reservation:
        """Book an estimate before a GPU call, or raise LimitExceededError."""
        usage = Usage(gpu_seconds=max(1, round(estimate_seconds)))
        result = await self._store.reserve("gpu:images", self._limits, usage, self._clock())
        if isinstance(result, Blocked):
            raise LimitExceededError(result.retry_after_seconds, "day")
        return result

    async def settle(self, reservation: Reservation, actual_seconds: float) -> None:
        """Replace the estimate with what the call took (0 when it failed before using the GPU)."""
        delta = max(0, round(actual_seconds)) - reservation.usage.gpu_seconds
        await self._store.adjust(reservation, "gpu_seconds", delta)
