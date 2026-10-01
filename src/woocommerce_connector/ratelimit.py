"""Request budget over a sliding 60 second window.

Honest note on WooCommerce, because this differs from most SaaS APIs:
**WooCommerce core ships no rate limiter for the `wc/v3` REST API.** The
throttling a connector actually meets in production comes from three other
places, and all three are handled here:

1. The **host** (WP Engine, Kinsta, SiteGround, Cloudflare in front of the
   store) returns `429` or `503` with `Retry-After`.
2. The **Store API** (`wc/store`) has optional rate limiting which, when a
   merchant enables it, emits the IETF draft headers `RateLimit-Limit`,
   `RateLimit-Remaining` and `RateLimit-Reset`.
3. Nothing throttles at all, and an impatient agent walks 40 pages of orders
   and takes the merchant's shop down. This is the real risk on self-hosted
   WooCommerce, so the budget below is enforced **client side** regardless of
   what the store says.

We therefore start from a deliberately conservative self-imposed budget,
raise it only when the store tells us a real limit, and always keep a reserve
so the merchant's other integrations are not starved.

Interface used by `WooClient`:

    entry, wait = await rl.acquire(cost)   # entry None => sleep `wait` seconds
    await rl.settle(entry, headers)        # learn the real limit from headers
    await rl.stats()
"""

from __future__ import annotations

import time
import uuid
from collections import deque
from collections.abc import Callable
from typing import Any

# Conservative default for a self-hosted store on shared hosting. Raised only
# if the store advertises a real limit through RateLimit-* headers.
DEFAULT_LIMIT_PER_MIN = 120
WINDOW_S = 60
DEFAULT_RESERVE = 0.2  # keep 20% of the budget for the merchant's other apps


def _num(v: str | None) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def request_cost(path: str, params: dict[str, Any] | None = None) -> int:
    """Requests, not credits: WooCommerce charges no per-field cost.

    One page is one request. Embedding (`_embed`) makes WordPress assemble
    related resources in the same response, which is heavier on the store even
    though it is still one HTTP call, so we count it as two.
    """
    params = params or {}
    return 2 if params.get("_embed") else 1


class RateLimiter:
    """In-process sliding-window budget. Correct for a single replica.

    For several replicas sharing one store, this must be replaced by a shared
    backend; see docs/ARCHITECTURE.md. The interface is intentionally the same
    shape so that swap is a constructor change.
    """

    def __init__(
        self,
        limit_per_min: int = DEFAULT_LIMIT_PER_MIN,
        *,
        reserve: float = DEFAULT_RESERVE,
        now: Callable[[], float] = time.monotonic,
    ):
        self.limit = limit_per_min
        self.reserve = reserve
        self._now = now
        self._events: deque[tuple[float, int]] = deque()  # (timestamp, cost)
        self._inflight: dict[str, int] = {}
        self.learned_limit: int | None = None
        self.last_remaining: float | None = None

    @property
    def usable(self) -> int:
        """Budget we allow ourselves, after holding back the reserve."""
        base = self.learned_limit or self.limit
        return max(1, int(base * (1.0 - self.reserve)))

    def _prune(self) -> None:
        cutoff = self._now() - WINDOW_S
        while self._events and self._events[0][0] <= cutoff:
            self._events.popleft()

    def _used(self) -> int:
        self._prune()
        return sum(c for _, c in self._events) + sum(self._inflight.values())

    async def acquire(self, cost: int = 1) -> tuple[str | None, float]:
        """Reserve `cost` against the window, or report how long to wait."""
        if self._used() + cost <= self.usable:
            entry = uuid.uuid4().hex
            self._inflight[entry] = cost
            return entry, 0.0
        # Wait until the oldest event leaves the window.
        if not self._events:
            return None, 1.0
        wait = max(0.0, (self._events[0][0] + WINDOW_S) - self._now())
        return None, max(wait, 0.05)

    async def settle(self, entry: str | None, headers: Any = None) -> None:
        """Commit a reservation and learn the store's real limit, if it says."""
        if entry is not None:
            cost = self._inflight.pop(entry, 1)
            self._events.append((self._now(), cost))
        if not headers:
            return
        get = headers.get if hasattr(headers, "get") else (lambda k, d=None: None)
        limit = _num(get("ratelimit-limit") or get("x-ratelimit-limit"))
        remaining = _num(get("ratelimit-remaining") or get("x-ratelimit-remaining"))
        if limit and limit > 0:
            self.learned_limit = int(limit)
        if remaining is not None:
            self.last_remaining = remaining

    async def stats(self) -> dict:
        used = self._used()
        return {
            "window_seconds": WINDOW_S,
            "limit_per_min": self.learned_limit or self.limit,
            "limit_source": "store headers" if self.learned_limit else "connector default",
            "usable_after_reserve": self.usable,
            "used_in_window": used,
            "remaining_in_window": max(0, self.usable - used),
            "store_reported_remaining": self.last_remaining,
        }
