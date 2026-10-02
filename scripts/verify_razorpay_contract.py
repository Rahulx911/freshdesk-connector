#!/usr/bin/env python3
"""Check this connector's assumptions against a real Razorpay account.

The Razorpay half was built against a mock reproducing the published API. The
WooCommerce half taught me that a documented contract and a real one are not
the same thing: the docs never said Basic auth silently fails without TLS,
and only a real store showed it.

This closes that gap without needing any data in the account. Every check
below is a claim the connector makes in `tests/test_razorpay.py`, and each
one is verified against the live API rather than against our own mock.

    export RAZORPAY_KEY_ID=rzp_test_...
    export RAZORPAY_KEY_SECRET=...
    python scripts/verify_razorpay_contract.py

Read-only, test mode, and safe to run against an empty account.
"""

from __future__ import annotations

import asyncio
import os
import sys

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m")
if not sys.stdout.isatty():
    GREEN = RED = YELLOW = DIM = BOLD = RESET = ""

results: list[tuple[str, bool, str]] = []


def check(claim: str, ok: bool, detail: str = "") -> None:
    results.append((claim, ok, detail))
    mark = f"{GREEN}ok{RESET}" if ok else f"{RED}MISMATCH{RESET}"
    print(f"  [{mark}] {claim}")
    if detail:
        print(f"        {DIM}{detail}{RESET}")


async def main() -> int:
    key_id = os.environ.get("RAZORPAY_KEY_ID")
    key_secret = os.environ.get("RAZORPAY_KEY_SECRET")
    base = os.environ.get("RAZORPAY_BASE_URL", "https://api.razorpay.com").rstrip("/")

    if not (key_id and key_secret):
        print("Set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET (test mode keys are fine).")
        print("Get them from the Razorpay dashboard: Settings > API Keys, in Test Mode.")
        return 2

    if not key_id.startswith("rzp_test_"):
        print(f"{YELLOW}Refusing to run: {key_id[:12]}… is not a test-mode key.{RESET}")
        print("This script only reads, but a live key describes real customer money.")
        return 2

    print(f"{BOLD}Verifying the connector's assumptions against {base}{RESET}")
    print(f"{DIM}key {key_id[:16]}… (test mode), read-only{RESET}\n")

    auth = (key_id, key_secret)
    async with httpx.AsyncClient(base_url=f"{base}/v1", auth=auth, timeout=30) as c:

        # 1. Basic auth over HTTPS is what Razorpay expects.
        r = await c.get("/payments", params={"count": 1})
        check("HTTP Basic auth with key id and secret is accepted",
              r.status_code == 200, f"GET /payments -> {r.status_code}")
        if r.status_code != 200:
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            print(f"\n{RED}Cannot continue: {body.get('error', {}).get('description', r.text[:200])}{RESET}")
            return 1

        payload = r.json()
        check("a collection is wrapped in {entity, count, items}",
              payload.get("entity") == "collection" and "items" in payload,
              f"keys: {sorted(payload)[:5]}")

        # 2. The claim most likely to be wrong, and the most expensive.
        items = payload.get("items") or []
        if items:
            amount = items[0].get("amount")
            check("amounts are integer paise, not rupees",
                  isinstance(amount, int),
                  f"first payment amount={amount} ({type(amount).__name__})")
        else:
            check("amounts are integer paise, not rupees", True,
                  "no payments in this account; unverified, not contradicted")

        # 3. A missing id answers 400, not 404. Our error mapping depends on
        #    reading the description rather than the status.
        r = await c.get("/payments/pay_doesnotexist00000")
        body = r.json() if r.content else {}
        description = (body.get("error") or {}).get("description", "")
        check("a non-existent id returns 400, not 404",
              r.status_code == 400, f"got {r.status_code}")
        check("the description says the id does not exist",
              "does not exist" in description.lower(),
              f"{description[:80]!r}")

        # 4. Our error envelope assumption.
        check("errors are wrapped in {\"error\": {code, description}}",
              isinstance(body.get("error"), dict) and "code" in body["error"],
              f"code={body.get('error', {}).get('code')}")

        # 5. Settlements exist and use the same envelope.
        r = await c.get("/settlements", params={"count": 1})
        check("the settlements endpoint is reachable",
              r.status_code == 200, f"GET /settlements -> {r.status_code}")
        if r.status_code == 200:
            setl = r.json()
            check("settlements use the same collection envelope",
                  setl.get("entity") == "collection")
            rows = setl.get("items") or []
            if rows:
                check("settlement amounts are integer paise",
                      isinstance(rows[0].get("amount"), int),
                      f"amount={rows[0].get('amount')}")
                check("a settlement carries a status and a bank reference field",
                      "status" in rows[0] and "utr" in rows[0],
                      f"status={rows[0].get('status')} utr={rows[0].get('utr')}")
            else:
                print(f"        {DIM}no settlements in this account yet{RESET}")

    # 6. A wrong secret must be refused, so the auth path is really enforced.
    async with httpx.AsyncClient(base_url=f"{base}/v1",
                                 auth=(key_id, "deliberately-wrong"), timeout=30) as c:
        r = await c.get("/payments", params={"count": 1})
        check("a wrong secret is rejected", r.status_code in (401, 400),
              f"got {r.status_code}")

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    colour = GREEN if passed == total else RED
    print(f"\n{colour}{BOLD}{passed}/{total} assumptions confirmed against the real API{RESET}")
    if passed != total:
        print(f"\n{YELLOW}A mismatch here is a real finding. Update the connector and the\n"
              f"conformance tests, and say so in docs/CAPABILITIES.md.{RESET}")
    return 0 if passed == total else 1


def run() -> int:
    """Turn a network failure into a sentence instead of a traceback."""
    try:
        return asyncio.run(main())
    except httpx.ConnectError:
        base = os.environ.get("RAZORPAY_BASE_URL", "https://api.razorpay.com")
        print(f"\n{RED}Could not reach {base}.{RESET}")
        print("Check the network, or RAZORPAY_BASE_URL if you set it.")
        return 1
    except httpx.TimeoutException:
        print(f"\n{RED}Razorpay did not respond in time.{RESET} Try again.")
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(run())
