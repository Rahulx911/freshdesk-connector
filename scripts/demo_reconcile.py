#!/usr/bin/env python3
"""The end-to-end story: two connectors, one grounded answer.

An agent in Agent Studio would have both MCP servers attached. This drives
them exactly as the agent would, and shows the thing neither system can say
alone: whether the customer actually got their money.

    python scripts/demo_reconcile.py
"""

from __future__ import annotations

import asyncio
import json
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

import os  # noqa: E402

from mock_razorpay import data as rzp_data  # noqa: E402
from mock_server import data as woo_data  # noqa: E402

BOLD, DIM, GREEN, RED, YELLOW, RESET = (
    "\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[33m", "\033[0m")
if not sys.stdout.isatty():
    BOLD = DIM = GREEN = RED = YELLOW = RESET = ""

checks: list[tuple[str, bool]] = []


def check(label: str, ok: bool) -> None:
    checks.append((label, bool(ok)))
    print(f"    [{GREEN + 'ok' + RESET if ok else RED + 'FAIL' + RESET}] {label}")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextmanager
def serve(module: str):
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", module, "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "error"],
        cwd=ROOT,
    )
    try:
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.1)
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


async def call(session: ClientSession, tool: str, args: dict) -> dict:
    """Call a tool and fail loudly with the connector's own message.

    Without this a structured error turns into a KeyError three frames deep
    inside an anyio task group, which hides the thing you need to read.
    """
    res = await session.call_tool(tool, args)
    payload = json.loads(res.content[0].text if res.content else "{}")
    if isinstance(payload, dict) and payload.get("error"):
        raise SystemExit(
            f"\n{RED}{tool} failed: {payload['error']}{RESET}\n"
            f"  {payload.get('message')}\n  hint: {payload.get('hint')}"
        )
    return payload


async def run(woo_url: str, rzp_url: str) -> None:
    woo_env = {**os.environ, "LOG_LEVEL": "ERROR", "AUDIT_LOG": "off",
               "WOO_STORE_URL": woo_url, "WOO_CONSUMER_KEY": woo_data.CONSUMER_KEY,
               "WOO_CONSUMER_SECRET": woo_data.CONSUMER_SECRET}
    rzp_env = {**os.environ, "LOG_LEVEL": "ERROR", "AUDIT_LOG": "off",
               "RAZORPAY_BASE_URL": rzp_url, "RAZORPAY_KEY_ID": rzp_data.KEY_ID,
               "RAZORPAY_KEY_SECRET": rzp_data.KEY_SECRET}

    woo_params = StdioServerParameters(
        command=sys.executable, args=["-m", "woocommerce_connector.cli", "serve"], env=woo_env)
    rzp_params = StdioServerParameters(
        command=sys.executable, args=["-m", "razorpay_connector.cli", "serve"], env=rzp_env)

    async with (
        stdio_client(woo_params) as (wr, ww), ClientSession(wr, ww) as woo,
        stdio_client(rzp_params) as (rr, rw), ClientSession(rr, rw) as rzp,
    ):
        wi = await woo.initialize()
        ri = await rzp.initialize()
        wt = await woo.list_tools()
        rt = await rzp.list_tools()
        print(f"{BOLD}Two connectors attached, as an Agent Studio agent would have them{RESET}")
        print(f"  {wi.serverInfo.name}: {len(wt.tools)} tools")
        print(f"  {ri.serverInfo.name}: {len(rt.tools)} tools")
        check("both connectors expose read-only tools",
              len(wt.tools) == 11 and len(rt.tools) == 10)

        mode = await call(rzp, "razorpay_status", {})
        check("gateway key mode is reported", mode.get("razorpay", {}).get("mode") == "test")

        # ------------------------------------------------------------------
        print(f"\n{BOLD}{'=' * 66}{RESET}")
        print(f"{BOLD}Q1. \"Where is my refund?\"  (order 1102){RESET}")
        print(f"{BOLD}{'=' * 66}{RESET}")

        print(f"\n{DIM}step 1 — the shop{RESET}")
        order = await call(woo, "get_order", {"order_id": 1102})
        sig = order["signals"]
        print(f"  status            : {order['status']}")
        print(f"  shop refunded     : INR {sig['amounts']['refunded_total']}")
        print(f"  shop verdict      : {sig['reconciliation']['status']}")
        check("shop reports the refund as unconfirmed",
              sig["reconciliation"]["status"] == "refund_not_confirmed_at_gateway")

        pay_id = sig["payment_refs"]["payment_id"][0]
        print(f"  payment reference : {pay_id}")

        print(f"\n{DIM}step 2 — the gateway{RESET}")
        verified = await call(rzp, "verify_refund", {
            "payment_id": pay_id,
            "shop_refunded_amount": sig["amounts"]["refunded_total"],
        })
        rec = verified["reconciliation"]
        print(f"  payment status    : {verified['payment']['status']}")
        print(f"  gateway refunded  : INR {rec.get('gateway_refunded')}")
        print(f"  {BOLD}{RED}verdict           : {rec['verdict']}{RESET}")
        check("gateway proves the refund was never issued", rec["verdict"] == "never_issued")

        print(f"\n{DIM}step 3 — the answer{RESET}")
        print(f"  {YELLOW}to the customer:{RESET} {rec['customer_safe_message']}")
        print(f"  {YELLOW}to the merchant:{RESET} {rec['action']}")
        check("the customer is not told the money was sent",
              "has been sent" not in rec["customer_safe_message"].lower())
        check("a human action is named", bool(rec.get("action")))

        # ------------------------------------------------------------------
        print(f"\n{BOLD}{'=' * 66}{RESET}")
        print(f"{BOLD}Q2. \"Was I charged twice?\"  (order 1103){RESET}")
        print(f"{BOLD}{'=' * 66}{RESET}")

        order3 = await call(woo, "get_order", {"order_id": 1103})
        ids = order3["signals"]["payment_refs"]["payment_id"]
        print(f"\n{DIM}step 1 — the shop sees two payment ids{RESET}")
        print(f"  {', '.join(ids)}")
        print(f"  shop verdict      : {order3['signals']['reconciliation']['status']}")
        print(f"  {DIM}(two ids can just be a retry; the shop cannot tell){RESET}")

        print(f"\n{DIM}step 2 — the gateway{RESET}")
        dup = await call(rzp, "verify_duplicate_charge", {"payment_ids": ids})
        d = dup["duplicate_charge"]
        print(f"  both captured     : {d['confirmed']}")
        print(f"  total captured    : INR {d['total_captured']}")
        print(f"  {BOLD}{RED}overcharged by    : INR {d['duplicate_amount']}{RESET}")
        check("duplicate capture confirmed at the gateway", d["confirmed"] is True)
        check("the overcharged amount is quantified", d["duplicate_amount"] > 0)

        print(f"\n{DIM}step 3 — the answer{RESET}")
        print(f"  {YELLOW}to the customer:{RESET} {dup['customer_safe_message']}")

        # ------------------------------------------------------------------
        print(f"\n{BOLD}{'=' * 66}{RESET}")
        print(f"{BOLD}Q3. A refund that IS confirmed  (order 1106){RESET}")
        print(f"{BOLD}{'=' * 66}{RESET}")

        order6 = await call(woo, "get_order", {"order_id": 1106})
        s6 = order6["signals"]
        ok = await call(rzp, "verify_refund", {
            "payment_id": s6["payment_refs"]["payment_id"][0],
            "shop_refunded_amount": s6["amounts"]["refunded_total"],
        })
        r6 = ok["reconciliation"]
        print(f"\n  shop refunded     : INR {s6['amounts']['refunded_total']}")
        print(f"  gateway refunded  : INR {r6.get('gateway_refunded')}")
        print(f"  {BOLD}{GREEN}verdict           : {r6['verdict']}{RESET}")
        print(f"\n  {YELLOW}to the customer:{RESET} {r6['customer_safe_message']}")
        check("a genuine refund is confirmed, not flagged", r6["verdict"] == "confirmed")
        check("the confirmation quotes a traceable bank reference",
              "reference" in r6["customer_safe_message"].lower())

        # ------------------------------------------------------------------
        print(f"\n{BOLD}{'=' * 66}{RESET}")
        print(f"{BOLD}Q4. \"Razorpay says it sent us X, the bank shows Y\"{RESET}")
        print(f"{BOLD}{'=' * 66}{RESET}")

        setls = await call(rzp, "list_settlements", {})
        print(f"\n  settlements          : {setls['count']}")
        if setls.get("not_yet_at_the_bank"):
            print(f"  {YELLOW}not yet at the bank  : "
                  f"{setls['not_yet_at_the_bank']['count']}{RESET}")
        check("settlements not yet at the bank are flagged",
              bool(setls.get("not_yet_at_the_bank")))

        recon = await call(rzp, "reconcile_settlement",
                           {"settlement_id": "setl_NmA1bCdEfGhIjK"})
        print(f"\n  payments in transfer : {recon['payments_in_settlement']}")
        print(f"  gross captured       : INR {recon['gross_captured']}")
        print(f"  deducted             : INR {recon['deducted']}")
        print(f"  net credited         : INR {recon['net_credited']}")
        print(f"  {DIM}{recon['explanation']['arithmetic']}{RESET}")
        print(f"  {DIM}{recon['explanation']['how_to_find_it_on_the_statement']}{RESET}")
        check("the settlement ties out to its payments",
              recon["gross_captured"] > recon["net_credited"])
        check("the deduction is explained, not just stated",
              "fees" in recon["explanation"]["arithmetic"])

        disputed = await call(rzp, "find_settlement_for_payment",
                              {"payment_id": "pay_NrT4bM9cPqWxYz"})
        print(f"\n  the disputed refund's payment has been paid out: {disputed['settled']}")
        check("a disputed payment can be traced to a payout",
              disputed["settled"] is True)

        # ------------------------------------------------------------------
        print(f"\n{BOLD}{'=' * 66}{RESET}")
        print(f"{BOLD}Q5. The merchant's exposure, in money{RESET}")
        print(f"{BOLD}{'=' * 66}{RESET}")
        pulse = await call(woo, "store_pulse", {"days": 30})
        risk = pulse["money_at_risk"]
        print(f"\n  unconfirmed refunds : {risk['unconfirmed_refunds']['orders']} orders, "
              f"INR {risk['unconfirmed_refunds']['amount']}")
        print(f"  possible double charges: {risk['possible_double_charges']['orders']} orders, "
              f"INR {risk['possible_double_charges']['order_value']}")
        # The key name changes on a partial scan, which is the point: a
        # lower bound must not read as a total.
        exposed = risk.get("total_exposed", risk.get("total_exposed_lower_bound"))
        label = "total exposed" if "total_exposed" in risk else "exposed (LOWER BOUND)"
        print(f"  {BOLD}{label:<20}: INR {exposed}{RESET}")
        print(f"  {DIM}scan covered {pulse['coverage']['orders_scanned']} of "
              f"{pulse['coverage']['orders_matching_window']} orders in the window{RESET}")
        check("exposure is reported as money, not categories", exposed > 0)
        check("a complete scan reports a total, not a lower bound", "total_exposed" in risk)

        cod = pulse.get("cash_on_delivery")
        if cod:
            print(f"  uncollected cash    : INR {cod['uncollected_value']}")
            print(f"  worth converting    : {len(cod['worth_converting_to_prepaid'])} order(s)")
            check("uncollected cash is reported separately from money at risk",
                  "total_exposed" in risk and cod["uncollected_value"] > 0)


def main() -> int:
    with serve("mock_server.app:app") as woo_url, serve("mock_razorpay.app:app") as rzp_url:
        print(f"{DIM}mock WooCommerce at {woo_url}{RESET}")
        print(f"{DIM}mock Razorpay    at {rzp_url}{RESET}\n")
        asyncio.run(run(woo_url, rzp_url))

    passed = sum(1 for _, ok in checks if ok)
    total = len(checks)
    colour = GREEN if passed == total else RED
    print(f"\n{colour}{BOLD}{passed}/{total} checks passed{RESET}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
