"""A small time-bounded response cache for GETs.

Why this exists: an agent answering one customer often asks the same thing
two or three times inside a single conversation, and `store_pulse` followed
by `get_order` on a ranked result re-fetches an order the connector just
read. Each of those is a live request against a shop that, as the rate
limiter's docstring says, may be on shared hosting.

Deliberately conservative, because stale order data is worse than slow order
data when the subject is someone's money:

* short default time to live, measured in seconds, not minutes;
* only successful GETs are stored, never errors;
* the cache key includes the credential, so two merchants can never see each
  other's rows through a shared process;
* authentication parameters that change per request (the OAuth nonce,
  timestamp and signature) are excluded from the key, or nothing would ever
  hit;
* an explicit `fresh=True` bypasses it for the cases where staleness is not
  acceptable.
"""

from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

DEFAULT_TTL_S = 30.0
DEFAULT_MAX_ENTRIES = 256

# These vary on every signed request and must not enter the key.
_VOLATILE_PARAMS = frozenset({
    "oauth_nonce", "oauth_timestamp", "oauth_signature",
    "oauth_consumer_key", "oauth_signature_method",
    "consumer_key", "consumer_secret",
})


def cache_key(credential_id: str, path: str, params: dict[str, Any]) -> str:
    stable = sorted((k, str(v)) for k, v in params.items() if k not in _VOLATILE_PARAMS)
    raw = "|".join([credential_id, path, *(f"{k}={v}" for k, v in stable)])
    return hashlib.sha256(raw.encode()).hexdigest()


class ResponseCache:
    """Least-recently-used cache with a per-entry expiry."""

    def __init__(
        self,
        ttl_s: float = DEFAULT_TTL_S,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        *,
        now: Callable[[], float] = time.monotonic,
    ):
        self.ttl_s = ttl_s
        self.max_entries = max_entries
        self._now = now
        self._entries: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> tuple[bool, Any]:
        """Returns (hit, value). A miss and a cached None are distinguishable."""
        entry = self._entries.get(key)
        if entry is None:
            self.misses += 1
            return False, None
        expires_at, value = entry
        if self._now() >= expires_at:
            del self._entries[key]
            self.misses += 1
            return False, None
        self._entries.move_to_end(key)
        self.hits += 1
        return True, value

    def put(self, key: str, value: Any) -> None:
        if self.ttl_s <= 0:
            return
        self._entries[key] = (self._now() + self.ttl_s, value)
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    def clear(self) -> None:
        self._entries.clear()

    def stats(self) -> dict:
        looked_up = self.hits + self.misses
        return {
            "ttl_seconds": self.ttl_s,
            "entries": len(self._entries),
            "max_entries": self.max_entries,
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hits / looked_up, 3) if looked_up else None,
        }
