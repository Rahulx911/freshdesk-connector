"""Low-level async HTTP client for Freshdesk API v2.

Rate-limit strategy (Freshdesk limits are per *account* per minute and
shared by every integration the merchant runs, so we must be polite):

* Proactive: a sliding 60s window sized from the account's real limit
  (learned from the `X-RateLimit-Total` header, conservative default until
  then). We keep a safety reserve so this connector never drains the whole
  quota the merchant's other integrations depend on.
* Reactive: on 429 we honour `Retry-After`, then retry.
* Bounded: one tool call never blocks longer than `max_wait_s`. If the
  required wait is longer we fail fast with a structured `RateLimited`
  error carrying `retry_after_seconds`, so the agent can tell the user
  instead of hanging the conversation.
* Transient 5xx / network errors: exponential backoff with jitter. Safe
  because the connector only issues GETs (idempotent).
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import random
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from .auth import Credentials
from .errors import (
    AuthError,
    FreshdeskError,
    InvalidRequest,
    NotFound,
    PermissionDenied,
    RateLimited,
    UpstreamError,
)
from .ratelimit import DEFAULT_LIMIT_PER_MIN, RateLimiter, RedisRateLimiter, request_cost

__all__ = ["DEFAULT_LIMIT_PER_MIN", "FreshdeskClient", "RateLimiter", "RedisRateLimiter", "request_cost"]

log = logging.getLogger("freshdesk_connector")

USER_AGENT = "agent-studio-freshdesk-connector/0.2"

# Credits spent by the current tool call (read by the observability layer).
credits_spent: contextvars.ContextVar[int] = contextvars.ContextVar("credits_spent", default=0)
upstream_calls: contextvars.ContextVar[int] = contextvars.ContextVar("upstream_calls", default=0)


_LINK_NEXT_RE = re.compile(r'<([^>]+)>;\s*rel="next"')


class FreshdeskClient:
    def __init__(
        self,
        creds: Credentials,
        *,
        rate_limiter: Any = None,  # RateLimiter | RedisRateLimiter (same async interface)
        max_wait_s: float = 20.0,
        max_retries: int = 3,
        timeout_s: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.creds = creds
        self.rl = rate_limiter or RateLimiter()
        self.max_wait_s = max_wait_s
        self.max_retries = max_retries
        self._sleep = sleep
        self._http = httpx.AsyncClient(
            base_url=creds.base_url,
            headers={
                "Authorization": creds.auth_header(),
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
            timeout=timeout_s,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> FreshdeskClient:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ core
    async def get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        """GET with throttling, Retry-After handling and bounded backoff."""
        params = {k: v for k, v in (params or {}).items() if v is not None}
        cost = request_cost(params)
        deadline = time.monotonic() + self.max_wait_s
        attempt = 0
        while True:
            entry, wait = await self.rl.acquire(cost)
            if entry is None:
                if time.monotonic() + wait > deadline:
                    raise RateLimited(
                        "Connector-side rate budget exhausted for this minute",
                        retry_after=wait, status=None,
                    )
                log.info("throttling %.1fs before %s", wait, path)
                await self._sleep(wait)
                continue
            credits_spent.set(credits_spent.get() + cost)
            upstream_calls.set(upstream_calls.get() + 1)
            try:
                resp = await self._http.get(path, params=params)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                attempt += 1
                if attempt > self.max_retries:
                    raise UpstreamError(f"Network error talking to Freshdesk: {type(e).__name__}") from e
                await self._backoff(attempt, deadline)
                continue

            await self.rl.settle(entry, resp.headers)

            if resp.status_code == 429:
                retry_after = _parse_retry_after(resp.headers.get("retry-after"))
                attempt += 1
                if attempt > self.max_retries or time.monotonic() + retry_after > deadline:
                    raise RateLimited("Freshdesk returned 429 Too Many Requests", retry_after=retry_after)
                log.warning("429 from Freshdesk; sleeping %.1fs (attempt %d)", retry_after, attempt)
                await self._sleep(retry_after)
                continue

            if resp.status_code >= 500:
                attempt += 1
                if attempt > self.max_retries:
                    raise UpstreamError(f"Freshdesk returned {resp.status_code}", status=resp.status_code)
                await self._backoff(attempt, deadline)
                continue

            if resp.status_code >= 400:
                raise _map_client_error(resp)
            return resp

    async def _backoff(self, attempt: int, deadline: float) -> None:
        # jitter for retry backoff; not used for anything security-sensitive
        delay = min(8.0, 0.5 * 2 ** (attempt - 1)) * (0.5 + random.random())  # nosec B311
        if time.monotonic() + delay > deadline:
            raise UpstreamError("Freshdesk kept failing; gave up within the time budget")
        await self._sleep(delay)

    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return (await self.get(path, params)).json()

    async def get_page(self, path: str, params: dict[str, Any] | None = None) -> tuple[Any, bool]:
        """Returns (json, has_next_page) using Freshdesk's `Link: <...>; rel="next"` header."""
        resp = await self.get(path, params)
        has_next = bool(_LINK_NEXT_RE.search(resp.headers.get("link", "")))
        return resp.json(), has_next

    async def whoami(self) -> dict:
        return await self.get_json("/api/v2/agents/me")


def _parse_retry_after(value: str | None) -> float:
    try:
        return max(1.0, float(value)) if value else 60.0
    except ValueError:
        return 60.0


def _map_client_error(resp: httpx.Response) -> FreshdeskError:
    try:
        body = resp.json()
    except ValueError:
        body = None
    details = None
    if isinstance(body, dict):
        details = body.get("errors") or body.get("description") or body.get("message")
    status = resp.status_code
    if status == 401:
        return AuthError("Freshdesk rejected the API key", status=status)
    if status == 403:
        return PermissionDenied("Access denied for this API key", status=status, details=details)
    if status == 404:
        return NotFound("Resource not found", status=status)
    return InvalidRequest(f"Freshdesk returned {status}", status=status, details=details)
