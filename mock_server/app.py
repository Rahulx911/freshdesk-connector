"""A WooCommerce REST API test double.

Faithful where it matters for a connector:

* the `/wp-json/wc/v3` namespace,
* HTTP Basic auth *and* `consumer_key`/`consumer_secret` query parameters,
* `X-WP-Total`, `X-WP-TotalPages` and `Link: rel="next"` pagination headers,
* the real error envelope `{"code", "message", "data": {"status"}}`,
* `per_page` capped at 100, with WooCommerce's own 400 when it is exceeded,
* optional throttling so rate-limit handling can be tested end to end.

Run it with `uvicorn mock_server.app:app --port 8787`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import data

app = FastAPI(title="Mock WooCommerce")

MAX_PER_PAGE = 100
RATE_LIMIT = int(os.environ.get("MOCK_RATE_LIMIT", "0"))   # 0 = unlimited
_hits: list[float] = []


def err(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse({"code": code, "message": message, "data": {"status": status}},
                        status_code=status)


def _rfc3986(value: Any) -> str:
    return quote(str(value), safe="")


def _oauth_authorized(request: Request) -> bool:
    """Verify an OAuth 1.0a one-legged signature the way WooCommerce does.

    Mirrors WC_REST_Authentication::check_oauth_signature(), including the two
    quirks: each `key=value` pair is RFC 3986 encoded whole and the pairs are
    joined with %26, and the signing key is `consumer_secret + "&"`.
    """
    params = dict(request.query_params)
    provided = params.pop("oauth_signature", None)
    if not provided or params.get("oauth_consumer_key") != data.CONSUMER_KEY:
        return False
    method = params.get("oauth_signature_method", "")
    algo = {"HMAC-SHA256": hashlib.sha256, "HMAC-SHA1": hashlib.sha1}.get(method)
    if algo is None:
        return False

    url = str(request.url).split("?", 1)[0]
    ordered = sorted(params.items())
    pairs = [_rfc3986(f"{_rfc3986(k)}={_rfc3986(v)}") for k, v in ordered]
    base = f"{request.method.upper()}&{_rfc3986(url)}&{'%26'.join(pairs)}"
    expected = base64.b64encode(
        hmac.new((data.CONSUMER_SECRET + "&").encode(), base.encode(), algo).digest()
    ).decode()
    return hmac.compare_digest(expected, provided)


def _authorized(request: Request) -> bool:
    # Real WooCommerce only accepts Basic auth over TLS and otherwise falls
    # through to OAuth. The mock accepts both so either path can be tested.
    header = request.headers.get("authorization", "")
    if header.lower().startswith("basic "):
        try:
            decoded = base64.b64decode(header[6:]).decode()
        except Exception:
            return False
        user, _, pwd = decoded.partition(":")
        if user == data.CONSUMER_KEY and pwd == data.CONSUMER_SECRET:
            return True
    if "oauth_signature" in request.query_params:
        return _oauth_authorized(request)
    key = request.query_params.get("consumer_key")
    secret = request.query_params.get("consumer_secret")
    if key and secret:
        return key == data.CONSUMER_KEY and secret == data.CONSUMER_SECRET
    return False


@app.middleware("http")
async def gate(request: Request, call_next):
    if not request.url.path.startswith("/wp-json/wc/v3"):
        return await call_next(request)

    if RATE_LIMIT:
        now = time.monotonic()
        _hits[:] = [t for t in _hits if now - t < 60]
        if len(_hits) >= RATE_LIMIT:
            retry = max(1, int(60 - (now - _hits[0])))
            return JSONResponse(
                {"code": "too_many_requests", "message": "Too many requests.",
                 "data": {"status": 429}},
                status_code=429,
                headers={"Retry-After": str(retry), "RateLimit-Limit": str(RATE_LIMIT),
                         "RateLimit-Remaining": "0"},
            )
        _hits.append(now)

    if not _authorized(request):
        return err("woocommerce_rest_authentication_error",
                   "Consumer key or secret is invalid.", 401)
    return await call_next(request)


def paginate(rows: list[dict], request: Request) -> tuple[list[dict], dict] | JSONResponse:
    try:
        page = int(request.query_params.get("page", 1))
        per_page = int(request.query_params.get("per_page", 10))
    except ValueError:
        return err("rest_invalid_param", "Invalid parameter(s): per_page", 400)
    if per_page > MAX_PER_PAGE or per_page < 1:
        return err("rest_invalid_param",
                   f"Invalid parameter(s): per_page. per_page must be between 1 and {MAX_PER_PAGE}.", 400)
    if page < 1:
        return err("rest_invalid_param", "Invalid parameter(s): page", 400)

    total = len(rows)
    total_pages = max(1, (total + per_page - 1) // per_page)
    start = (page - 1) * per_page
    window = rows[start:start + per_page]
    headers = {"X-WP-Total": str(total), "X-WP-TotalPages": str(total_pages)}
    if page < total_pages:
        url = str(request.url.include_query_params(page=page + 1))
        headers["Link"] = f'<{url}>; rel="next"'
    return window, headers


def _matches_order(o: dict, p: Any) -> bool:
    status = p.get("status")
    if status and status != "any":
        if o.get("status") not in status.split(","):
            return False
    after, before = p.get("after"), p.get("before")
    created = o.get("date_created_gmt") or ""
    if after and created < after:
        return False
    if before and created > before:
        return False
    modified_after = p.get("modified_after")
    if modified_after and (o.get("date_modified_gmt") or "") < modified_after:
        return False
    customer = p.get("customer")
    if customer not in (None, "") and str(o.get("customer_id")) != str(customer):
        return False
    product = p.get("product")
    if product not in (None, ""):
        if not any(str(i.get("product_id")) == str(product) for i in o.get("line_items", [])):
            return False
    search = (p.get("search") or "").lower()
    if search:
        b = o.get("billing") or {}
        hay = " ".join(str(x) for x in (
            o.get("number"), b.get("email"), b.get("first_name"), b.get("last_name"))).lower()
        if search not in hay:
            return False
    return True


@app.get("/wp-json/wc/v3/orders")
async def list_orders(request: Request):
    p = dict(request.query_params)
    rows = [o for o in data.ORDERS if _matches_order(o, p)]
    rows.sort(key=lambda o: o.get("date_created_gmt") or "",
              reverse=p.get("order", "desc") != "asc")
    out = paginate(rows, request)
    if isinstance(out, JSONResponse):
        return out
    window, headers = out
    return JSONResponse(window, headers=headers)


@app.get("/wp-json/wc/v3/orders/{order_id}")
async def get_order(order_id: int):
    for o in data.ORDERS:
        if o["id"] == order_id:
            return JSONResponse(o)
    return err("woocommerce_rest_invalid_id", "Invalid ID.", 404)


@app.get("/wp-json/wc/v3/orders/{order_id}/refunds")
async def order_refunds(order_id: int, request: Request):
    if not any(o["id"] == order_id for o in data.ORDERS):
        return err("woocommerce_rest_invalid_id", "Invalid ID.", 404)
    out = paginate(data.REFUNDS.get(order_id, []), request)
    if isinstance(out, JSONResponse):
        return out
    window, headers = out
    return JSONResponse(window, headers=headers)


@app.get("/wp-json/wc/v3/products")
async def list_products(request: Request):
    p = dict(request.query_params)
    rows = list(data.PRODUCTS)
    status = p.get("status")
    if status and status != "any":
        rows = [r for r in rows if r.get("status") in status.split(",")]
    if p.get("stock_status"):
        rows = [r for r in rows if r.get("stock_status") == p["stock_status"]]
    if p.get("sku"):
        rows = [r for r in rows if p["sku"].lower() in (r.get("sku") or "").lower()]
    if p.get("search"):
        s = p["search"].lower()
        rows = [r for r in rows if s in (r.get("name") or "").lower()
                or s in (r.get("sku") or "").lower()]
    out = paginate(rows, request)
    if isinstance(out, JSONResponse):
        return out
    window, headers = out
    return JSONResponse(window, headers=headers)


@app.get("/wp-json/wc/v3/products/{product_id}")
async def get_product(product_id: int):
    for p in data.PRODUCTS:
        if p["id"] == product_id:
            return JSONResponse(p)
    return err("woocommerce_rest_invalid_id", "Invalid ID.", 404)


@app.get("/wp-json/wc/v3/customers")
async def list_customers(request: Request):
    p = dict(request.query_params)
    rows = list(data.CUSTOMERS)
    if p.get("email"):
        rows = [c for c in rows if (c.get("email") or "").lower() == p["email"].lower()]
    if p.get("search"):
        s = p["search"].lower()
        rows = [c for c in rows if s in " ".join(
            str(x) for x in (c.get("first_name"), c.get("last_name"), c.get("email"))).lower()]
    out = paginate(rows, request)
    if isinstance(out, JSONResponse):
        return out
    window, headers = out
    return JSONResponse(window, headers=headers)


@app.get("/wp-json/wc/v3/customers/{customer_id}")
async def get_customer(customer_id: int):
    for c in data.CUSTOMERS:
        if c["id"] == customer_id:
            return JSONResponse(c)
    return err("woocommerce_rest_invalid_id", "Invalid ID.", 404)


@app.get("/wp-json/wc/v3/{rest:path}")
async def unknown(rest: str):
    return err("rest_no_route", f"No route was found matching the URL: /wc/v3/{rest}", 404)
