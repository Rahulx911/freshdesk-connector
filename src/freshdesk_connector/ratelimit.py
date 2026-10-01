"""Rate budgets, counted in Freshdesk API *credits* over a sliding 60s window.

Freshdesk limits are per account per minute and shared by every app the
merchant has installed. Two interchangeable backends:

* `RateLimiter`      in-process; right for one replica (or stdio mode).
* `RedisRateLimiter` shared by every replica that talks to the same
                     Freshdesk domain; check-and-reserve is one atomic Lua
                     script that uses Redis server time, so replicas with
                     skewed clocks still agree.

Both expose the same async interface used by `FreshdeskClient`:
    entry, wait = await rl.acquire(cost)   # entry None => wait `wait` seconds
    await rl.settle(entry, headers)        # learn limit/remaining, fix actual charge
    await rl.stats()
"""

from __future__ import annotations

import time
import uuid
from collections import deque
from collections.abc import Callable
from typing import Any

import httpx

DEFAULT_LIMIT_PER_MIN = 50  # lowest Freshdesk plan/trial limit; replaced once headers are seen
WINDOW_S = 60


def _num(v: str | None) -> float | None:
    """Freshdesk sends rate-limit headers as decimals ("700.0"), so parse as float."""
    if v is None:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def request_cost(params: dict[str, Any] | None) -> int:
    """API credits a call consumes. Freshdesk docs: each `include` costs 2 extra
    credits (include=stats -> 3 total); include=conversations costs 1 extra."""
    inc = [x for x in str((params or {}).get("include") or "").split(",") if x]
    return 1 + sum(1 if x == "conversations" else 2 for x in inc)


def _parse_headers(headers: httpx.Headers) -> tuple[int | None, int | None, int | None]:
    total = _num(headers.get("x-ratelimit-total"))
    remaining = _num(headers.get("x-ratelimit-remaining"))
    used = _num(headers.get("x-ratelimit-used-currentrequest"))
    return (int(total) if total and total > 0 else None,
            int(remaining) if remaining is not None else None,
            int(used) if used else None)


class RateLimiter:
    """In-process sliding window. Safe under asyncio concurrency because
    `acquire` has no await between the check and the reservation."""

    backend = "memory"

    def __init__(
        self,
        limit_per_min: int = DEFAULT_LIMIT_PER_MIN,
        reserve_fraction: float = 0.2,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.limit = limit_per_min
        self.reserve_fraction = reserve_fraction
        self.clock = clock
        self._sent: deque[list] = deque()   # [timestamp, credits]; mutable so a charge can be corrected
        self.server_remaining: int | None = None
        self._remaining_at: float = 0.0     # when server_remaining was observed

    @property
    def budget(self) -> int:
        """Credits per rolling minute this connector allows itself."""
        return max(1, int(self.limit * (1 - self.reserve_fraction)))

    def _trim(self, now: float) -> None:
        while self._sent and now - self._sent[0][0] >= WINDOW_S:
            self._sent.popleft()

    def used(self) -> int:
        self._trim(self.clock())
        return sum(c for _, c in self._sent)

    def required_wait(self, cost: int = 1) -> float:
        now = self.clock()
        self._trim(now)
        waits = [0.0]
        cost = min(cost, self.budget)
        used = sum(c for _, c in self._sent)
        if used + cost > self.budget:
            need, freed = used + cost - self.budget, 0
            for ts, c in self._sent:
                freed += c
                if freed >= need:
                    waits.append(WINDOW_S - (now - ts))
                    break
        # The shared account quota is nearly gone (merchant's other apps): back off
        # until the window that observation belongs to has rolled over. A stale
        # observation (older than one window) says nothing about now.
        fresh = now - self._remaining_at < WINDOW_S
        if (fresh and self.server_remaining is not None
                and self.server_remaining < max(cost, int(self.limit * 0.05))):
            waits.append(max(1.0, WINDOW_S - (now - self._remaining_at)))
        return max(waits)

    def record(self, cost: int = 1) -> list:
        entry = [self.clock(), cost]
        self._sent.append(entry)
        return entry

    async def acquire(self, cost: int) -> tuple[list | None, float]:
        wait = self.required_wait(cost)
        if wait > 0:
            return None, wait
        return self.record(cost), 0.0

    def observe_headers(self, headers: httpx.Headers, entry: list | None = None) -> None:
        total, remaining, used = _parse_headers(headers)
        if total:
            self.limit = total
        if remaining is not None:
            self.server_remaining = remaining
            self._remaining_at = self.clock()
        if used and entry is not None:
            entry[1] = used                 # correct our estimate with the real charge

    async def settle(self, entry: Any, headers: httpx.Headers) -> None:
        self.observe_headers(headers, entry)

    def snapshot(self) -> dict:
        return {
            "backend": self.backend,
            "account_limit_per_min": self.limit,
            "connector_budget_per_min": self.budget,
            "credits_used_last_60s": self.used(),
            "server_reported_remaining": self.server_remaining,
        }

    async def stats(self) -> dict:
        return self.snapshot()


# KEYS[1] window zset, KEYS[2] state hash (limit, remaining)
# ARGV: cost, member_id, default_limit, reserve_fraction
# returns {granted(0/1), wait_ms, used_after, budget}
_ACQUIRE_LUA = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local window = 60000
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - window)
local limit = tonumber(redis.call('HGET', KEYS[2], 'limit') or ARGV[3])
local budget = math.max(1, math.floor(limit * (1 - tonumber(ARGV[4]))))
local cost = math.min(tonumber(ARGV[1]), budget)
local members = redis.call('ZRANGE', KEYS[1], 0, -1, 'WITHSCORES')
local used = 0
for i = 1, #members, 2 do
  used = used + tonumber(string.match(members[i], ':(%d+)$'))
end
local wait = 0
if used + cost > budget then
  local need, freed = used + cost - budget, 0
  for i = 1, #members, 2 do
    freed = freed + tonumber(string.match(members[i], ':(%d+)$'))
    if freed >= need then
      wait = tonumber(members[i + 1]) + window - now
      break
    end
  end
end
local remaining = redis.call('HGET', KEYS[2], 'remaining')
local seen_at = tonumber(redis.call('HGET', KEYS[2], 'remaining_at') or '0')
if remaining and now - seen_at < window
   and tonumber(remaining) < math.max(cost, math.floor(limit * 0.05)) then
  wait = math.max(wait, 1000, seen_at + window - now)
end
if wait > 0 then
  return {0, wait, used, budget}
end
redis.call('ZADD', KEYS[1], now, ARGV[2] .. ':' .. cost)
redis.call('PEXPIRE', KEYS[1], window * 2)
return {1, 0, used + cost, budget}
"""


class RedisRateLimiter:
    """Budget shared by all replicas for one Freshdesk domain."""

    backend = "redis"

    def __init__(self, redis: Any, domain_key: str, *, reserve_fraction: float = 0.2,
                 default_limit: int = DEFAULT_LIMIT_PER_MIN, prefix: str = "fdconn"):
        self.r = redis
        self.reserve_fraction = reserve_fraction
        self.default_limit = default_limit
        self.k_window = f"{prefix}:rl:{domain_key}:window"
        self.k_state = f"{prefix}:rl:{domain_key}:state"
        self._script = redis.register_script(_ACQUIRE_LUA)

    async def acquire(self, cost: int) -> tuple[str | None, float]:
        member = uuid.uuid4().hex
        granted, wait_ms, _used, _budget = await self._script(
            keys=[self.k_window, self.k_state],
            args=[cost, member, self.default_limit, self.reserve_fraction],
        )
        if int(granted):
            return f"{member}:{min(cost, int(_budget))}", 0.0
        return None, int(wait_ms) / 1000.0

    async def settle(self, entry: str | None, headers: httpx.Headers) -> None:
        total, remaining, used = _parse_headers(headers)
        pipe = self.r.pipeline()
        if total:
            pipe.hset(self.k_state, "limit", total)
        if remaining is not None:
            sec, usec = await self.r.time()
            pipe.hset(self.k_state, mapping={"remaining": remaining,
                                             "remaining_at": int(sec) * 1000 + int(usec) // 1000})
        pipe.expire(self.k_state, 120)
        if used and entry:
            member_id, est = entry.rsplit(":", 1)
            if int(est) != used:
                score = await self.r.zscore(self.k_window, entry)
                if score is not None:
                    pipe.zrem(self.k_window, entry)
                    pipe.zadd(self.k_window, {f"{member_id}:{used}": score})
        await pipe.execute()

    async def stats(self) -> dict:
        members = await self.r.zrange(self.k_window, 0, -1)
        state = await self.r.hgetall(self.k_state)
        limit = int(state.get(b"limit") or state.get("limit") or self.default_limit)
        remaining = state.get(b"remaining") or state.get("remaining")
        used = sum(int((m.decode() if isinstance(m, bytes) else m).rsplit(":", 1)[1]) for m in members)
        return {
            "backend": self.backend,
            "account_limit_per_min": limit,
            "connector_budget_per_min": max(1, int(limit * (1 - self.reserve_fraction))),
            "credits_used_last_60s": used,   # approximate: includes entries about to expire
            "server_reported_remaining": int(remaining) if remaining is not None else None,
        }
