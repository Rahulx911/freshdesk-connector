#!/usr/bin/env python3
"""Prove, against a live store, that the configured key cannot write.

This deliberately does not go through the connector: it signs a POST by hand,
so it tests the *credential*, not our restraint. A Read-scoped WooCommerce key
must be refused by the store itself.
"""

from __future__ import annotations

import asyncio
import os
import sys

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from woocommerce_connector.oauth import sign


async def main() -> int:
    store = os.environ.get("WOO_STORE_URL", "http://localhost:8080").rstrip("/")
    key = os.environ.get("WOO_CONSUMER_KEY")
    secret = os.environ.get("WOO_CONSUMER_SECRET")
    if not (key and secret):
        print("Set WOO_CONSUMER_KEY and WOO_CONSUMER_SECRET")
        return 2

    url = f"{store}/wp-json/wc/v3/orders"
    async with httpx.AsyncClient(timeout=30) as c:
        resp = await c.post(url, params=sign("POST", url, {}, key, secret),
                            json={"status": "pending"})

    body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    print(f"POST /orders -> {resp.status_code} {body.get('code')}")
    print(f"  {body.get('message')}")

    if resp.status_code in (200, 201):
        print("\nFAIL: the store accepted a write. This key is not Read-scoped.")
        return 1
    if resp.status_code in (401, 403):
        print("\nok: the store refused the write at the credential level")
        return 0
    print(f"\nFAIL: unexpected status {resp.status_code}")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
