"""A Razorpay API test double, faithful where a connector can get it wrong.

* HTTP Basic auth with key_id as the username and key_secret as the password.
* Amounts in **paise**, as the real API returns them.
* Razorpay's error envelope: `{"error": {"code", "description", ...}}`,
  which is a different shape from WooCommerce's.
* `GET /v1/payments/{id}/refunds` returns a collection envelope
  `{"entity": "collection", "count": N, "items": [...]}`, not a bare array.
* Optional throttling so 429 handling can be exercised.

Run it with `uvicorn mock_razorpay.app:app --port 8788`.
"""

from __future__ import annotations

import base64
import os
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import data

app = FastAPI(title="Mock Razorpay")

RATE_LIMIT = int(os.environ.get("MOCK_RZP_RATE_LIMIT", "0"))
_hits: list[float] = []


def err(code: str, description: str, status: int, reason: str = "input_validation_failed") -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "description": description, "source": "business",
                   "step": "payment_initiation", "reason": reason, "metadata": {}}},
        status_code=status,
    )


def _authorized(request: Request) -> bool:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:]).decode()
    except Exception:
        return False
    user, _, pwd = decoded.partition(":")
    return user == data.KEY_ID and pwd == data.KEY_SECRET


@app.middleware("http")
async def gate(request: Request, call_next):
    if not request.url.path.startswith("/v1/"):
        return await call_next(request)

    if RATE_LIMIT:
        now = time.monotonic()
        _hits[:] = [t for t in _hits if now - t < 60]
        if len(_hits) >= RATE_LIMIT:
            return JSONResponse(
                {"error": {"code": "BAD_REQUEST_ERROR",
                           "description": "Too many requests", "reason": "rate_limit_exceeded"}},
                status_code=429, headers={"Retry-After": "30"},
            )
        _hits.append(now)

    if not _authorized(request):
        return err("BAD_REQUEST_ERROR", "Authentication failed", 401, "authentication_failed")
    return await call_next(request)


@app.get("/v1/payments/{payment_id}")
async def get_payment(payment_id: str):
    payment = data.PAYMENTS.get(payment_id)
    if not payment:
        return err("BAD_REQUEST_ERROR", f"The id provided does not exist: {payment_id}", 400)
    return JSONResponse(payment)


@app.get("/v1/payments/{payment_id}/refunds")
async def payment_refunds(payment_id: str):
    if payment_id not in data.PAYMENTS:
        return err("BAD_REQUEST_ERROR", f"The id provided does not exist: {payment_id}", 400)
    ids = data.REFUNDS_BY_PAYMENT.get(payment_id, [])
    items = [data.REFUNDS[r] for r in ids]
    return JSONResponse({"entity": "collection", "count": len(items), "items": items})


@app.get("/v1/refunds/{refund_id}")
async def get_refund(refund_id: str):
    refund = data.REFUNDS.get(refund_id)
    if not refund:
        return err("BAD_REQUEST_ERROR", f"The id provided does not exist: {refund_id}", 400)
    return JSONResponse(refund)


@app.get("/v1/{rest:path}")
async def unknown(rest: str):
    return err("BAD_REQUEST_ERROR", f"The requested URL was not found: /v1/{rest}", 404)
