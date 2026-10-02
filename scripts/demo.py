#!/usr/bin/env python3
"""End-to-end demo: drive the connector over MCP exactly as an agent would.

    python scripts/demo.py           # starts the bundled mock WooCommerce
    python scripts/demo.py --live    # uses WOO_STORE_URL / WOO_CONSUMER_KEY /
                                     # WOO_CONSUMER_SECRET

In --live mode it is still read-only: nothing in the store changes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"
if not sys.stdout.isatty():
    GREEN = RED = DIM = BOLD = RESET = ""

checks: list[tuple[str, bool]] = []


def check(label: str, ok: bool) -> None:
    checks.append((label, bool(ok)))
    mark = f"{GREEN}ok{RESET}" if ok else f"{RED}FAIL{RESET}"
    print(f"    [{mark}] {label}")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextmanager
def mock_store(rate_limit: int = 0):
    """Run the mock WooCommerce on a free port. rate_limit=0 means unlimited."""
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "mock_server.app:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "error"],
        cwd=ROOT,
        env={**os.environ, "MOCK_RATE_LIMIT": str(rate_limit)},
    )
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.1)
        yield base
    finally:
        proc.terminate()
        proc.wait(timeout=10)


async def call(session: ClientSession, tool: str, args: dict) -> dict | None:
    result = await session.call_tool(tool, args)
    text = result.content[0].text if result.content else "{}"
    try:
        payload = json.loads(text)
    except ValueError:
        print(f"    {RED}{tool}: non-JSON response{RESET}")
        return None
    if isinstance(payload, dict) and payload.get("error"):
        print(f"    {DIM}{tool} -> {payload['error']}: {payload['message']}{RESET}")
        return payload
    return payload


async def run(store_url: str, key: str, secret: str, *, live: bool) -> None:
    env = {
        **os.environ,
        "LOG_LEVEL": "ERROR", "AUDIT_LOG": "off",
        "WOO_STORE_URL": store_url, "WOO_CONSUMER_KEY": key, "WOO_CONSUMER_SECRET": secret,
    }
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "woocommerce_connector.cli", "serve"], env=env
    )
    async with stdio_client(params) as (r, w), ClientSession(r, w) as session:
        init = await session.initialize()
        tools = await session.list_tools()
        print(f"{BOLD}Connected to '{init.serverInfo.name}' with {len(tools.tools)} tools{RESET}")
        print(f"{DIM}{', '.join(t.name for t in tools.tools)}{RESET}\n")
        check("11 read-only tools exposed", len(tools.tools) == 11)

        print(f"\n{BOLD}== Connection{RESET}")
        status = await call(session, "connector_status", {})
        check("connector reports connected", bool(status and status.get("connected")))
        check("read-only mode advertised", "read-only" in (status or {}).get("mode", ""))
        check("rate budget reported", bool((status or {}).get("rate_budget")))

        print(f"\n{BOLD}== Orders{RESET}")
        page = await call(session, "list_orders", {"per_page": 5})
        items = (page or {}).get("items", [])
        check("list_orders returned orders", bool(items))
        check("pagination metadata present", "total_matching" in (page or {}))
        check("every order carries signals", all("signals" in o for o in items))
        check("every order carries a source_url", all("source_url" in o for o in items))

        failed = await call(session, "search_orders", {"status": ["failed", "pending"]})
        check("status filter works", bool((failed or {}).get("items")))

        print(f"\n{BOLD}== Payment signals (the differentiator){RESET}")
        pulse = await call(session, "store_pulse", {"days": 30})
        coverage = (pulse or {}).get("coverage", {})
        check("store_pulse scanned orders", bool(coverage.get("orders_scanned")))
        check("scan coverage is reported", "scan_complete" in coverage)
        attention = (pulse or {}).get("needs_attention", [])
        check("needs_attention is ranked", bool(attention))
        check("every ranked order explains itself", all(e.get("why") for e in attention))

        unconfirmed = (pulse or {}).get("refunds_not_confirmed_at_gateway", [])
        if not live:
            check("unconfirmed refund detected", bool(unconfirmed))
        if attention:
            top = attention[0]
            print(f"\n    {BOLD}top of the backlog:{RESET} order {top.get('number')} "
                  f"(score {top.get('score')})")
            for reason in top.get("why", []):
                print(f"      · {reason}")

        if not live:
            detail = await call(session, "get_order", {"order_id": 1102})
            sig = (detail or {}).get("signals", {})
            recon = sig.get("reconciliation", {})
            check("refund without a gateway id is flagged",
                  recon.get("status") == "refund_not_confirmed_at_gateway")
            check("the flag explains the risk in words", bool(recon.get("explanation")))

            double = await call(session, "get_order", {"order_id": 1103})
            dsig = (double or {}).get("signals", {})
            check("double charge detected",
                  dsig.get("reconciliation", {}).get("status") == "multiple_payments")
            check("Razorpay payment ids extracted",
                  len(dsig.get("payment_refs", {}).get("payment_id", [])) == 2)

            paid = await call(session, "get_order", {"order_id": 1101})
            check("signature verification surfaced",
                  (paid or {}).get("signals", {}).get("payment_refs", {}).get("signature_verified") is True)

        print(f"\n{BOLD}== Customers{RESET}")
        if not live:
            hist = await call(session, "customer_order_history", {"email": "rohit.bansal@example.com"})
            check("guest checkout resolved by email", (hist or {}).get("matched_by") == "guest_email")
            check("guest fallback is explained", bool((hist or {}).get("note")))
            acct = await call(session, "customer_order_history", {"customer_id": 31})
            check("registered customer history", (acct or {}).get("summary", {}).get("orders") == 2)

        print(f"\n{BOLD}== Safety{RESET}")
        if not live:
            injected = await call(session, "get_order", {"order_id": 1108})
            check("prompt injection flagged",
                  "override_instructions" in (injected or {}).get("untrusted_text_flags", []))
            check("PII masked by default",
                  "@" in (injected or {}).get("billing", {}).get("email", "")
                  and "*" in (injected or {}).get("billing", {}).get("email", ""))

        print(f"\n{BOLD}== Failure modes{RESET}")
        missing = await call(session, "get_order", {"order_id": 987654321})
        check("unknown id -> structured not_found", (missing or {}).get("error") == "not_found")
        bad = await call(session, "search_orders", {"status": ["nonsense"]})
        check("invalid filter -> structured invalid_request",
              (bad or {}).get("error") == "invalid_request")
        check("errors carry an actionable hint", bool((bad or {}).get("hint")))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true",
                    help="run against a real store from WOO_STORE_URL / WOO_CONSUMER_KEY / WOO_CONSUMER_SECRET")
    args = ap.parse_args()

    if args.live:
        url = os.environ.get("WOO_STORE_URL")
        key = os.environ.get("WOO_CONSUMER_KEY")
        secret = os.environ.get("WOO_CONSUMER_SECRET")
        if not (url and key and secret):
            print("Set WOO_STORE_URL, WOO_CONSUMER_KEY and WOO_CONSUMER_SECRET for --live")
            return 2
        print(f"{BOLD}Live mode against {url} (read-only){RESET}\n")
        asyncio.run(run(url, key, secret, live=True))
    else:
        from mock_server import data as mock_data
        with mock_store() as base:
            print(f"{BOLD}Mock WooCommerce at {base}{RESET}\n")
            asyncio.run(run(base, mock_data.CONSUMER_KEY, mock_data.CONSUMER_SECRET, live=False))

    passed = sum(1 for _, ok in checks if ok)
    total = len(checks)
    colour = GREEN if passed == total else RED
    print(f"\n{colour}{BOLD}{passed}/{total} checks passed{RESET}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
