"""Quota counters. Redis in production (Upstash) and locally; an in-memory store for unit tests.

Each limit is a fixed-window counter keyed by model, unit and window start, expiring when the window ends.
`reserve` checks every limit and increments them all atomically (a Lua script in Redis), so concurrent
workers cannot overshoot a limit between the check and the increment.
"""

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
    # Counter keys charged, so the reservation can be corrected later in the same windows.
    keys: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Blocked:
    model: str
    limit: Limit
    retry_after_seconds: float


def window_key(model: str, limit: Limit, now: float) -> tuple[str, float]:
    """Counter key for the window containing `now`, and the seconds until that window ends."""
    start = int(now // limit.window_seconds) * limit.window_seconds
    return (
        f"{KEY_PREFIX}:{model}:{limit.unit}:{limit.window_seconds}:{start}",
        start + limit.window_seconds - now,
    )


def cooldown_key(model: str) -> str:
    return f"{KEY_PREFIX}:{model}:cooldown"


class QuotaStore(Protocol):
    async def reserve(
        self, model: str, limits: list[Limit], usage: Usage, now: float
    ) -> Reservation | Blocked: ...

    async def adjust(self, reservation: Reservation, unit: str, delta: int) -> None: ...

    async def set_cooldown(self, model: str, seconds: float, now: float) -> None: ...

    async def cooldown_remaining(self, model: str, now: float) -> float: ...


# KEYS[i]: counter keys. ARGV: n, then per key: limit, amount, ttl_seconds.
# Returns 0 if all reserved, else the 1-based index of the first limit that would be exceeded.
_RESERVE_SCRIPT = """
local n = tonumber(ARGV[1])
for i = 1, n do
  local limit = tonumber(ARGV[1 + (i - 1) * 3 + 1])
  local amount = tonumber(ARGV[1 + (i - 1) * 3 + 2])
  local current = tonumber(redis.call('GET', KEYS[i]) or '0')
  if amount > 0 and current + amount > limit then
    return i
  end
end
for i = 1, n do
  local amount = tonumber(ARGV[1 + (i - 1) * 3 + 2])
  local ttl = tonumber(ARGV[1 + (i - 1) * 3 + 3])
  if amount > 0 then
    redis.call('INCRBY', KEYS[i], amount)
    if redis.call('TTL', KEYS[i]) < 0 then
      redis.call('EXPIRE', KEYS[i], ttl)
    end
  end
end
return 0
"""


class RedisQuotaStore:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._reserve = redis.register_script(_RESERVE_SCRIPT)

    async def reserve(
        self, model: str, limits: list[Limit], usage: Usage, now: float
    ) -> Reservation | Blocked:
        keys: list[str] = []
        args: list[int] = [len(limits)]
        remaining: list[float] = []
        for limit in limits:
            key, seconds_left = window_key(model, limit, now)
            keys.append(key)
            remaining.append(seconds_left)
            # TTL slightly past the window end so a counter never expires mid-window.
            args += [limit.amount, usage.amount(limit), int(seconds_left) + 5]
        blocked_index = int(await self._reserve(keys=keys, args=args))
        if blocked_index:
            i = blocked_index - 1
            return Blocked(model=model, limit=limits[i], retry_after_seconds=remaining[i])
        return Reservation(
            model=model,
            usage=usage,
            keys={limit.name: key for limit, key in zip(limits, keys, strict=True)},
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
        keyed = [(limit, *window_key(model, limit, now)) for limit in limits]
        for limit, key, seconds_left in keyed:
            amount = usage.amount(limit)
            if amount > 0 and self.counters.get(key, 0) + amount > limit.amount:
                return Blocked(model=model, limit=limit, retry_after_seconds=seconds_left)
        for limit, key, _ in keyed:
            self.counters[key] = self.counters.get(key, 0) + usage.amount(limit)
        return Reservation(model=model, usage=usage, keys={limit.name: key for limit, key, _ in keyed})

    async def adjust(self, reservation: Reservation, unit: str, delta: int) -> None:
        for name, key in reservation.keys.items():
            if name.startswith(f"{unit}/"):
                self.counters[key] = self.counters.get(key, 0) + delta

    async def set_cooldown(self, model: str, seconds: float, now: float) -> None:
        self.cooldown_until[model] = now + seconds

    async def cooldown_remaining(self, model: str, now: float) -> float:
        return max(0.0, self.cooldown_until.get(model, 0.0) - now)
