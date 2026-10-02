"""Low-level async HTTP client for the WooCommerce REST API (wc/v3).

Only GETs are ever issued. Combined with a Read-scoped consumer key, the
store cannot be modified by this connector even if this file is wrong.

Rate-limit strategy:
* Proactive: a client-side sliding 60s budget (see ratelimit.py), because
  WooCommerce core does not throttle and an agent paging through orders can
  otherwise overwhelm a shared-hosting store.
* Reactive: 429 and 503 both carry `Retry-After` in practice; we honour it.
* Bounded: one tool call never blocks longer than `max_wait_s`. Beyond that
  we fail fast with a structured `RateLimited` carrying `retry_after_seconds`
  so the agent can tell the user instead of hanging the conversation.
* Transient 5xx / network errors: exponential backoff with jitter, safe
  because every request is an idempotent GET.
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
from .cache import ResponseCache, cache_key
from .errors import (
    AuthError,
    InvalidRequest,
    NotFound,
    PermissionDenied,
    RateLimited,
    UpstreamError,
    WooError,
)
from .oauth import sign
from .ratelimit import DEFAULT_LIMIT_PER_MIN, RateLimiter, request_cost

__all__ = ["DEFAULT_LIMIT_PER_MIN", "RateLimiter", "WooClient", "request_cost"]

log = logging.getLogger("woocommerce_connector")

USER_AGENT = "agent-studio-woocommerce-connector/0.1"

requests_spent: contextvars.ContextVar[int] = contextvars.ContextVar("requests_spent", default=0)
cache_hits: contextvars.ContextVar[int] = contextvars.ContextVar("cache_hits", default=0)
upstream_calls: contextvars.ContextVar[int] = contextvars.ContextVar("upstream_calls", default=0)

_LINK_NEXT_RE = re.compile(r'<([^>]+)>;\s*rel="next"')

# WooCommerce error codes that mean "the credential is wrong", regardless of
# the HTTP status the host chose to attach to them.
_AUTH_CODES = {
    "woocommerce_rest_authentication_error",
    "woocommerce_rest_cannot_view",
    "rest_forbidden",
    "rest_cannot_view",
}


class WooClient:
    def __init__(
        self,
        creds: Credentials,
        *,
        rate_limiter: Any = None,
        cache: ResponseCache | None = None,
        max_wait_s: float = 20.0,
        max_retries: int = 3,
        timeout_s: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.creds = creds
        self.rl = rate_limiter or RateLimiter()
        self.cache = cache if cache is not None else ResponseCache()
        # Scoped to the credential so two merchants in one process can never
        # read each other's rows out of a shared cache.
        self._cache_scope = f"{creds.store_url}|{creds.consumer_key}"
        self.max_wait_s = max_wait_s
        self.max_retries = max_retries
        self._sleep = sleep
        auth = (creds.consumer_key, creds.consumer_secret) if creds.uses_basic_auth else None
        self._http = httpx.AsyncClient(
            base_url=creds.api_base(),
            auth=auth,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            timeout=timeout_s,
            transport=transport,
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> WooClient:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ core
    async def get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        if not self.creds.uses_basic_auth:
            # Plain HTTP: WooCommerce ignores Basic auth and requires a
            # signed request. Sign the absolute URL, query params included.
            params = sign(
                "GET",
                self.creds.api_base() + path,
                params,
                self.creds.consumer_key,
                self.creds.consumer_secret,
            )
        cost = request_cost(path, params)
        deadline = time.monotonic() + self.max_wait_s
        attempt = 0
        while True:
            entry, wait = await self.rl.acquire(cost)
            if entry is None:
                if time.monotonic() + wait > deadline:
                    raise RateLimited(
                        "Connector-side request budget exhausted for this minute",
                        retry_after=wait, status=None,
                    )
                log.info("throttling %.1fs before %s", wait, path)
                await self._sleep(wait)
                continue
            requests_spent.set(requests_spent.get() + cost)
            upstream_calls.set(upstream_calls.get() + 1)
            try:
                resp = await self._http.get(path, params=params)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                await self.rl.settle(entry, None)
                attempt += 1
                if attempt > self.max_retries:
                    raise UpstreamError(f"Network error talking to the store: {type(e).__name__}") from e
                await self._backoff(attempt, deadline)
                continue

            await self.rl.settle(entry, resp.headers)

            if resp.status_code in (429, 503):
                retry_after = _parse_retry_after(resp.headers.get("retry-after"))
                attempt += 1
                if attempt > self.max_retries or time.monotonic() + retry_after > deadline:
                    raise RateLimited(
                        f"Store returned {resp.status_code}; it is throttling or overloaded",
                        retry_after=retry_after, status=resp.status_code,
                    )
                log.warning("%d from store; sleeping %.1fs (attempt %d)", resp.status_code, retry_after, attempt)
                await self._sleep(retry_after)
                continue

            if resp.status_code >= 500:
                attempt += 1
                if attempt > self.max_retries:
                    raise UpstreamError(f"Store returned {resp.status_code}", status=resp.status_code)
                await self._backoff(attempt, deadline)
                continue

            if resp.status_code in (301, 302, 307, 308):
                # A store that redirects http->https or bare->www would leak the
                # credential to the redirect target; refuse rather than follow.
                raise InvalidRequest(
                    "Store redirected the API request. Configure the canonical store URL "
                    f"(it redirected to {resp.headers.get('location', '?')}).",
                    status=resp.status_code,
                )

            if resp.status_code >= 400:
                raise _map_client_error(resp)
            return resp

    async def _backoff(self, attempt: int, deadline: float) -> None:
        delay = min(8.0, 0.5 * 2 ** (attempt - 1)) * (0.5 + random.random())  # nosec B311
        if time.monotonic() + delay > deadline:
            raise UpstreamError("Store kept failing; gave up within the time budget")
        await self._sleep(delay)

    async def get_json(self, path: str, params: dict[str, Any] | None = None,
                       *, fresh: bool = False) -> Any:
        key = cache_key(self._cache_scope, path, params or {})
        if not fresh:
            hit, cached = self.cache.get(key)
            if hit:
                cache_hits.set(cache_hits.get() + 1)
                return cached
        body = (await self.get(path, params)).json()
        self.cache.put(key, body)
        return body

    async def get_page(self, path: str, params: dict[str, Any] | None = None,
                       *, fresh: bool = False) -> tuple[Any, dict]:
        """Returns (json, page_info).

        WordPress sends `X-WP-Total` and `X-WP-TotalPages` on collection
        endpoints, and a `Link: <...>; rel="next"` header. We report all three
        because `X-WP-Total` is what lets an agent say "312 matching orders"
        without walking every page.
        """
        key = cache_key(self._cache_scope, path, params or {})
        if not fresh:
            hit, cached = self.cache.get(key)
            if hit:
                cache_hits.set(cache_hits.get() + 1)
                return cached
        resp = await self.get(path, params)
        h = resp.headers
        info = {
            "total": _int(h.get("x-wp-total")),
            "total_pages": _int(h.get("x-wp-totalpages")),
            "has_more": bool(_LINK_NEXT_RE.search(h.get("link", ""))),
        }
        result = (resp.json(), info)
        self.cache.put(key, result)
        return result

    async def ping(self) -> dict:
        """Cheapest call that proves the credential can read orders."""
        data, info = await self.get_page("/orders", {"per_page": 1})
        return {"reachable": True, "orders_visible": info.get("total"), "sample": len(data or [])}


def _int(v: str | None) -> int | None:
    try:
        return int(v) if v not in (None, "") else None
    except ValueError:
        return None


def _parse_retry_after(value: str | None) -> float:
    try:
        return max(1.0, float(value)) if value else 60.0
    except ValueError:
        return 60.0


def _map_client_error(resp: httpx.Response) -> WooError:
    """WooCommerce errors are `{"code":..., "message":..., "data":{"status":...}}`.

    The HTTP status alone is not enough: a Read key hitting a write-only route
    and a revoked key can both surface as 401, and `woocommerce_rest_cannot_view`
    arrives as 401 on some hosts and 403 on others.
    """
    try:
        body = resp.json()
    except ValueError:
        body = None
    code = message = None
    if isinstance(body, dict):
        code = body.get("code")
        message = body.get("message")
    status = resp.status_code
    details = {"woocommerce_code": code} if code else None

    if code == "woocommerce_rest_invalid_id" or status == 404:
        return NotFound(message or "Resource not found", status=status, details=details)
    if code in _AUTH_CODES or status == 401:
        if code in ("woocommerce_rest_cannot_view", "rest_cannot_view", "rest_forbidden"):
            return PermissionDenied(
                message or "This API key may not read that resource", status=status, details=details
            )
        return AuthError(message or "The store rejected the consumer key/secret",
                         status=status, details=details)
    if status == 403:
        return PermissionDenied(message or "Access denied for this API key", status=status, details=details)
    return InvalidRequest(message or f"Store returned {status}", status=status, details=details)
