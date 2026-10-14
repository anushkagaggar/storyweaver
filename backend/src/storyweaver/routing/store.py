"""Quota counters. Redis in production (Upstash) and locally; an in-memory store for unit tests.

Groq enforces limits over a *rolling* window, so plain fixed windows let bursts through at the boundary (two
calls at 0:58 and one at 1:01 are three calls inside 60 s; seen live against safeguard's 3 RPM). Each limit
therefore uses a sliding-window estimate:

    used = current_window_count + previous_window_count * (share of the previous window still in range)

`reserve` checks every limit and increments them all atomically (a Lua script in Redis), so concurrent
workers cannot overshoot between the check and the increment.
"""

import math
from dataclasses import dataclass, field
from typing import Protocol

from redis.asyncio import Redis

from storyweaver.routing.quotas import Limit

KEY_PREFIX = "quota"


@dataclass(frozen=True)
class Usage:
    requests: int = 0
    tokens: int = 0
    audio_seconds: int = 0

    def amount(self, limit: Limit) -> int:
        return int(getattr(self, limit.unit))


@dataclass(frozen=True)
class Reservation:
    model: str
    usage: Usage
    # Current-window counter keys charged, so the reservation can be corrected later.
    keys: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Blocked:
    model: str
    limit: Limit
    retry_after_seconds: float


@dataclass(frozen=True)
class WindowKeys:
    current: str
    previous: str
    length: int
    previous_weight: float  # share of the previous window that still falls inside the rolling window
    seconds_left: float  # until the current fixed window ends

    @property
    def ttl_seconds(self) -> int:
        # The current counter is read again as the "previous" window, so it must outlive the next window.
        return int(self.seconds_left) + self.length + 5


def window_keys(model: str, limit: Limit, now: float) -> WindowKeys:
    length = limit.window_seconds
    start = int(now // length) * length
    base = f"{KEY_PREFIX}:{model}:{limit.unit}:{length}"
    return WindowKeys(
        current=f"{base}:{start}",
        previous=f"{base}:{start - length}",
        length=length,
        previous_weight=(start + length - now) / length,
        seconds_left=start + length - now,
    )


def estimated_usage(current: int, previous: int, keys: WindowKeys) -> float:
    return current + previous * keys.previous_weight


def retry_after(limit: Limit, amount: int, current: int, previous: int, keys: WindowKeys) -> float:
    """Seconds until `amount` fits: the previous window's share decays linearly to zero at the window end."""
    if current + amount > limit.amount or previous <= 0:
        return keys.seconds_left
    headroom = limit.amount - amount - current
    # Solve current + previous * (seconds_left - t) / length + amount <= limit for t.
    wait = keys.seconds_left - headroom * limit.window_seconds / previous
    return max(1.0, math.ceil(wait))


def cooldown_key(model: str) -> str:
    return f"{KEY_PREFIX}:{model}:cooldown"


class QuotaStore(Protocol):
    async def reserve(
        self, model: str, limits: list[Limit], usage: Usage, now: float
    ) -> Reservation | Blocked: ...

    async def adjust(self, reservation: Reservation, unit: str, delta: int) -> None: ...

    async def set_cooldown(self, model: str, seconds: float, now: float) -> None: ...

    async def cooldown_remaining(self, model: str, now: float) -> float: ...


# Per limit i: KEYS[2i-1] current counter, KEYS[2i] previous counter.
# ARGV: n, then per limit: limit, amount, ttl_seconds, previous_weight.
# Returns {0} if all reserved, else {index (1-based), current, previous} for the first limit exceeded.
_RESERVE_SCRIPT = """
local n = tonumber(ARGV[1])
for i = 1, n do
  local base = 1 + (i - 1) * 4
  local limit = tonumber(ARGV[base + 1])
  local amount = tonumber(ARGV[base + 2])
  local weight = tonumber(ARGV[base + 4])
  local current = tonumber(redis.call('GET', KEYS[2 * i - 1]) or '0')
  local previous = tonumber(redis.call('GET', KEYS[2 * i]) or '0')
  if amount > 0 and current + previous * weight + amount > limit then
    return {i, current, previous}
  end
end
for i = 1, n do
  local base = 1 + (i - 1) * 4
  local amount = tonumber(ARGV[base + 2])
  local ttl = tonumber(ARGV[base + 3])
  if amount > 0 then
    redis.call('INCRBY', KEYS[2 * i - 1], amount)
    if redis.call('TTL', KEYS[2 * i - 1]) < 0 then
      redis.call('EXPIRE', KEYS[2 * i - 1], ttl)
    end
  end
end
return {0}
"""


class RedisQuotaStore:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._reserve = redis.register_script(_RESERVE_SCRIPT)

    async def reserve(
        self, model: str, limits: list[Limit], usage: Usage, now: float
    ) -> Reservation | Blocked:
        windows = [window_keys(model, limit, now) for limit in limits]
        keys: list[str] = []
        args: list[str | int] = [len(limits)]
        for limit, window in zip(limits, windows, strict=True):
            keys += [window.current, window.previous]
            args += [limit.amount, usage.amount(limit), window.ttl_seconds, repr(window.previous_weight)]
        result = await self._reserve(keys=keys, args=args)
        index = int(result[0])
        if index:
            limit, window = limits[index - 1], windows[index - 1]
            wait = retry_after(limit, usage.amount(limit), int(result[1]), int(result[2]), window)
            return Blocked(model=model, limit=limit, retry_after_seconds=wait)
        return Reservation(
            model=model,
            usage=usage,
            keys={limit.name: window.current for limit, window in zip(limits, windows, strict=True)},
        )

    async def adjust(self, reservation: Reservation, unit: str, delta: int) -> None:
        if delta == 0:
            return
        pipe = self._redis.pipeline(transaction=False)
        for name, key in reservation.keys.items():
            if name.startswith(f"{unit}/"):
                pipe.incrby(key, delta)
        await pipe.execute()

    async def set_cooldown(self, model: str, seconds: float, now: float) -> None:
        await self._redis.set(cooldown_key(model), "1", px=max(1, int(seconds * 1000)))

    async def cooldown_remaining(self, model: str, now: float) -> float:
        ms = int(await self._redis.pttl(cooldown_key(model)))
        return ms / 1000 if ms > 0 else 0.0


class InMemoryQuotaStore:
    """Same semantics as RedisQuotaStore, for unit tests. Not shared between processes."""

    def __init__(self) -> None:
        self.counters: dict[str, int] = {}
        self.cooldown_until: dict[str, float] = {}

    async def reserve(
        self, model: str, limits: list[Limit], usage: Usage, now: float
    ) -> Reservation | Blocked:
        windows = [window_keys(model, limit, now) for limit in limits]
        for limit, window in zip(limits, windows, strict=True):
            amount = usage.amount(limit)
            current = self.counters.get(window.current, 0)
            previous = self.counters.get(window.previous, 0)
            if amount > 0 and estimated_usage(current, previous, window) + amount > limit.amount:
                wait = retry_after(limit, amount, current, previous, window)
                return Blocked(model=model, limit=limit, retry_after_seconds=wait)
        for limit, window in zip(limits, windows, strict=True):
            self.counters[window.current] = self.counters.get(window.current, 0) + usage.amount(limit)
        return Reservation(
            model=model,
            usage=usage,
            keys={limit.name: window.current for limit, window in zip(limits, windows, strict=True)},
        )

    async def adjust(self, reservation: Reservation, unit: str, delta: int) -> None:
        for name, key in reservation.keys.items():
            if name.startswith(f"{unit}/"):
                self.counters[key] = self.counters.get(key, 0) + delta

    async def set_cooldown(self, model: str, seconds: float, now: float) -> None:
        self.cooldown_until[model] = now + seconds

    async def cooldown_remaining(self, model: str, now: float) -> float:
        return max(0.0, self.cooldown_until.get(model, 0.0) - now)
