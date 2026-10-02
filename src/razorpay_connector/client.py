"""Async HTTP client for the Razorpay API. GETs only.

Shares the request-budget, backoff and bounded-wait behaviour of the
WooCommerce client, because the failure modes are the same shape. What
differs is the error envelope and, importantly, the units.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from woocommerce_connector.errors import (
    AuthError,
    InvalidRequest,
    NotFound,
    PermissionDenied,
    RateLimited,
    UpstreamError,
    WooError,
)
from woocommerce_connector.ratelimit import RateLimiter

from .auth import Credentials

log = logging.getLogger("razorpay_connector")

USER_AGENT = "agent-studio-razorpay-connector/0.1"

# Razorpay publishes a per-key request rate. Start below it and leave the
# merchant's own integrations room, exactly as on the store side.
DEFAULT_LIMIT_PER_MIN = 300


class RazorpayClient:
    def __init__(
        self,
        creds: Credentials,
        *,
        rate_limiter: Any = None,
        max_wait_s: float = 20.0,
        max_retries: int = 3,
        timeout_s: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.creds = creds
        self.rl = rate_limiter or RateLimiter(DEFAULT_LIMIT_PER_MIN)
        self.max_wait_s = max_wait_s
        self.max_retries = max_retries
        self._sleep = sleep
        self._http = httpx.AsyncClient(
            base_url=creds.api_base(),
            auth=(creds.key_id, creds.key_secret),
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            timeout=timeout_s,
            transport=transport,
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> RazorpayClient:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        deadline = time.monotonic() + self.max_wait_s
        attempt = 0
        while True:
            entry, wait = await self.rl.acquire(1)
            if entry is None:
                if time.monotonic() + wait > deadline:
                    raise RateLimited("Connector-side request budget exhausted",
                                      retry_after=wait, status=None)
                await self._sleep(wait)
                continue
            try:
                resp = await self._http.get(path, params=params or {})
            except (httpx.TimeoutException, httpx.TransportError) as e:
                await self.rl.settle(entry, None)
                attempt += 1
                if attempt > self.max_retries:
                    raise UpstreamError(f"Network error talking to Razorpay: {type(e).__name__}") from e
                await self._backoff(attempt, deadline)
                continue

            await self.rl.settle(entry, resp.headers)

            if resp.status_code == 429:
                retry_after = _retry_after(resp.headers.get("retry-after"))
                attempt += 1
                if attempt > self.max_retries or time.monotonic() + retry_after > deadline:
                    raise RateLimited("Razorpay is throttling this key",
                                      retry_after=retry_after, status=429)
                await self._sleep(retry_after)
                continue

            if resp.status_code >= 500:
                attempt += 1
                if attempt > self.max_retries:
                    raise UpstreamError(f"Razorpay returned {resp.status_code}",
                                        status=resp.status_code)
                await self._backoff(attempt, deadline)
                continue

            if resp.status_code >= 400:
                raise _map_error(resp)
            return resp.json()

    async def _backoff(self, attempt: int, deadline: float) -> None:
        delay = min(8.0, 0.5 * 2 ** (attempt - 1)) * (0.5 + random.random())  # nosec B311
        if time.monotonic() + delay > deadline:
            raise UpstreamError("Razorpay kept failing; gave up within the time budget")
        await self._sleep(delay)


def _retry_after(value: str | None) -> float:
    try:
        return max(1.0, float(value)) if value else 30.0
    except ValueError:
        return 30.0


def _map_error(resp: httpx.Response) -> WooError:
    """Razorpay wraps errors as {"error": {"code", "description", "reason"}}.

    It also returns **400, not 404**, for an id that does not exist, so the
    description has to be read to tell "no such payment" apart from "your
    request was malformed".
    """
    try:
        body = resp.json()
    except ValueError:
        body = {}
    error = (body or {}).get("error") or {}
    description = error.get("description") or f"Razorpay returned {resp.status_code}"
    reason = error.get("reason")
    details = {"razorpay_code": error.get("code"), "reason": reason} if error else None
    status = resp.status_code

    if status == 401 or reason == "authentication_failed":
        return AuthError("Razorpay rejected the key id/secret", status=status, details=details)
    if status == 403:
        return PermissionDenied("This Razorpay key may not read that resource",
                                status=status, details=details)
    if "does not exist" in description.lower() or status == 404:
        return NotFound(description, status=status, details=details)
    return InvalidRequest(description, status=status, details=details)
