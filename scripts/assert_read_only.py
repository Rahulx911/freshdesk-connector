#!/usr/bin/env python3
"""Prove, against a live store, that the configured key can read but cannot write.

Both halves matter, and the second is worthless without the first.

A key that does not exist is rejected with 401 for *every* request, writes
included. A naive check that only asserts "the write was refused" therefore
passes for a typo, an expired key, or no key at all, and reports a security
property the credential does not actually have. So this script:

  1. proves the key is valid and can read  (GET must succeed), then
  2. proves it cannot write                (POST must be refused), and
  3. distinguishes "refused because read-scoped" from "refused because the
     key is invalid", which look identical at the HTTP layer.

It signs the write by hand rather than going through the connector, so it
tests the credential rather than our restraint.
"""

from __future__ import annotations

import asyncio
import os
import sys

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from woocommerce_connector.oauth import sign

# Razorpay-style wording differs per host; these are WooCommerce's.
INVALID_KEY_MARKERS = ("consumer key is invalid", "authentication failed",
                       "invalid signature", "consumer secret is invalid")
READ_ONLY_MARKERS = ("does not have write permissions", "write permission")


def _body(resp: httpx.Response) -> dict:
    try:
        return resp.json()
    except ValueError:
        return {}


async def main() -> int:
    store = os.environ.get("WOO_STORE_URL", "http://localhost:8080").rstrip("/")
    key = os.environ.get("WOO_CONSUMER_KEY")
    secret = os.environ.get("WOO_CONSUMER_SECRET")
    if not (key and secret):
        print("Set WOO_CONSUMER_KEY and WOO_CONSUMER_SECRET")
        return 2

    base = f"{store}/wp-json/wc/v3"
    orders = f"{base}/orders"

    async with httpx.AsyncClient(timeout=30) as client:
        # ---- 1. the key must actually work for reads -------------------
        read = await client.get(orders, params=sign("GET", orders, {"per_page": 1}, key, secret))
        read_body = _body(read)
        print(f"GET  /orders -> {read.status_code}")
        if read.status_code != 200:
            print(f"  {read_body.get('message') or read_body.get('code')}")
            print("\nFAIL: the key cannot read, so this proves nothing about write scope.")
            print("      A key that is invalid or revoked is refused for everything,")
            print("      which would make the write check below pass for the wrong reason.")
            print("      Re-run bootstrap.sh and export the key it prints.")
            return 1
        print(f"  ok: {len(read_body)} order(s) visible, so the credential is valid")

        # ---- 2. the same key must be refused for writes ----------------
        write = await client.post(orders, params=sign("POST", orders, {}, key, secret),
                                  json={"status": "pending"})
        write_body = _body(write)
        message = str(write_body.get("message") or "")
        print(f"POST /orders -> {write.status_code} {write_body.get('code')}")
        print(f"  {message}")

    if write.status_code in (200, 201):
        print("\nFAIL: the store accepted a write. This key is not Read-scoped.")
        return 1
    if write.status_code not in (401, 403):
        print(f"\nFAIL: unexpected status {write.status_code}; cannot conclude anything.")
        return 1

    # ---- 3. refused for the right reason -------------------------------
    lowered = message.lower()
    if any(m in lowered for m in INVALID_KEY_MARKERS):
        print("\nFAIL: the write was refused because the credential was rejected, not")
        print("      because it lacks write permission. That is not the property we claim.")
        return 1
    if not any(m in lowered for m in READ_ONLY_MARKERS):
        print("\nWARNING: refused, but the message does not name write permission.")
        print("         Treat this as inconclusive rather than proof.")
        return 1

    print("\nok: the key reads successfully and the store refuses its writes,")
    print("    naming the missing write permission. Read-only is enforced by")
    print("    the credential, not by this codebase.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
