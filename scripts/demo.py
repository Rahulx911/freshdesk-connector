"""End-to-end demo over the real MCP protocol.

Spawns the connector as an MCP stdio server (exactly how an agent runtime
would), then calls every tool the way an agent would and prints compact
results, including the failure cases (bad id, bad filter, bad key).

    python scripts/demo.py              # starts the bundled mock Freshdesk
    python scripts/demo.py --live       # uses FRESHDESK_DOMAIN / FRESHDESK_API_KEY

In --live mode it is still read-only: nothing in your helpdesk changes.
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
MOCK_KEY = "mock-api-key-123"

GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"
if not sys.stdout.isatty():
    GREEN = RED = DIM = BOLD = RESET = ""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def mock_freshdesk(rate_limit: int):
    port = _free_port()
    env = {**os.environ, "MOCK_RATE_LIMIT": str(rate_limit)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "mock_server.app:app", "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env=env,
    )
    try:
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def _short(obj, limit=600) -> str:
    s = json.dumps(obj, ensure_ascii=False)
    return s if len(s) <= limit else s[:limit] + f"… ({len(s)} chars)"


async def call(session: ClientSession, name: str, args: dict, *, expect_error: bool = False) -> dict | None:
    res = await session.call_tool(name, args)
    text = res.content[0].text if res.content else ""
    ok = res.isError == expect_error
    mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
    print(f"\n[{mark}] {BOLD}{name}{RESET}({_short(args, 200)})"
          f"{'  (expected error)' if expect_error else ''}")
    try:
        payload = res.structuredContent or json.loads(text)
    except (ValueError, TypeError):
        payload = text
    if isinstance(payload, dict) and "result" in payload and len(payload) == 1:
        payload = payload["result"]
    print(f"{DIM}  → {_short(payload)}{RESET}")
    call.results.append(ok)
    return payload if isinstance(payload, dict) else None


call.results = []


async def run_session(base_url: str, api_key: str, *, live: bool) -> None:
    env = {**os.environ, "LOG_LEVEL": "ERROR", "AUDIT_LOG": "off", "FRESHDESK_DOMAIN": base_url, "FRESHDESK_API_KEY": api_key,
           "FRESHDESK_MAX_WAIT_S": "5"}
    params = StdioServerParameters(command=sys.executable,
                                   args=["-m", "freshdesk_connector.cli", "serve"], env=env)
    async with stdio_client(params) as (r, w), ClientSession(r, w) as session:
        init = await session.initialize()
        tools = await session.list_tools()
        print(f"{BOLD}Connected to MCP server '{init.serverInfo.name}' — {len(tools.tools)} tools:{RESET} "
              + ", ".join(t.name for t in tools.tools))

        print(f"\n{BOLD}== Auth / status{RESET}")
        await call(session, "connector_status", {})

        print(f"\n{BOLD}== List{RESET}")
        await call(session, "list_tickets", {"per_page": 5})
        page1 = await call(session, "list_tickets", {"updated_since": "2000-01-01T00:00:00Z", "per_page": 10})
        if page1 and page1.get("has_more"):
            await call(session, "list_tickets", {"updated_since": "2000-01-01T00:00:00Z", "per_page": 10,
                                                 "page": page1["next_page"]})

        print(f"\n{BOLD}== Search{RESET}")
        found = await call(session, "search_tickets", {"status": ["open", "pending"], "priority": ["urgent", "high"]})
        await call(session, "search_tickets", {"tags": ["refund", "payments"], "created_after": "2000-01-01"})

        print(f"\n{BOLD}== Get{RESET}")
        tid = (found or {}).get("items", [{}])[0].get("id", 1) if found and found.get("items") else 1
        await call(session, "get_ticket", {"ticket_id": tid, "max_conversations": 5})
        if not live:
            t5 = await call(session, "get_ticket", {"ticket_id": 5, "max_conversations": 10})
            if t5 and t5.get("conversations_truncated"):
                await call(session, "list_ticket_conversations", {"ticket_id": 5, "page": 2, "per_page": 10})

        print(f"\n{BOLD}== Customers & companies{RESET}")
        if not live:
            await call(session, "customer_ticket_history", {"email": "kabir.nair@example.com", "limit": 5})
            await call(session, "find_contacts", {"phone": "+91 90000 00005"})
            await call(session, "find_contacts", {"name": "Mee"})
            await call(session, "get_contact", {"contact_id": 1002})
            await call(session, "find_companies", {"name": "Br"})
            await call(session, "get_company", {"company_id": 501})

        print(f"\n{BOLD}== Failure cases (agent gets a structured error + hint){RESET}")
        await call(session, "get_ticket", {"ticket_id": 987654}, expect_error=True)
        await call(session, "search_tickets", {"tags": ["x' OR status:5"]}, expect_error=True)
        await call(session, "search_tickets", {"status": ["stuck"]}, expect_error=True)
        await call(session, "find_contacts", {"email": "a@b.co", "name": "x"}, expect_error=True)


async def run_bad_key(base_url: str) -> None:
    print(f"\n{BOLD}== Wrong API key{RESET}")
    env = {**os.environ, "LOG_LEVEL": "ERROR", "AUDIT_LOG": "off", "FRESHDESK_DOMAIN": base_url, "FRESHDESK_API_KEY": "not-a-real-key"}
    params = StdioServerParameters(command=sys.executable,
                                   args=["-m", "freshdesk_connector.cli", "serve"], env=env)
    async with stdio_client(params) as (r, w), ClientSession(r, w) as session:
        await session.initialize()
        await call(session, "connector_status", {}, expect_error=True)


async def run_rate_limit(base_url: str, api_key: str) -> None:
    print(f"\n{BOLD}== Rate limiting (mock account limit 10/min, connector keeps 20% reserve){RESET}")
    env = {**os.environ, "LOG_LEVEL": "ERROR", "AUDIT_LOG": "off", "FRESHDESK_DOMAIN": base_url, "FRESHDESK_API_KEY": api_key,
           "FRESHDESK_MAX_WAIT_S": "3"}
    params = StdioServerParameters(command=sys.executable,
                                   args=["-m", "freshdesk_connector.cli", "serve"], env=env)
    async with stdio_client(params) as (r, w), ClientSession(r, w) as session:
        await session.initialize()
        for i in range(10):
            res = await session.call_tool("get_company", {"company_id": 501})
            if res.isError:
                body = json.loads(res.content[0].text.split(": ", 1)[-1])
                print(f"  call {i + 1}: {RED}{body['error']}{RESET} retry_after={body.get('retry_after_seconds')}s "
                      f"{DIM}hint: {body['hint']}{RESET}")
                call.results.append(body["error"] == "rate_limited" and i >= 7)
                break
            print(f"  call {i + 1}: ok")
        else:
            call.results.append(False)
        status = await session.call_tool("connector_status", {})
        print(f"{DIM}  status → {status.content[0].text[:300]}{RESET}")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="use FRESHDESK_DOMAIN/FRESHDESK_API_KEY")
    args = ap.parse_args()

    if args.live:
        dom, key = os.environ.get("FRESHDESK_DOMAIN"), os.environ.get("FRESHDESK_API_KEY")
        if not (dom and key):
            print("Set FRESHDESK_DOMAIN and FRESHDESK_API_KEY for --live", file=sys.stderr)
            return 2
        await run_session(dom, key, live=True)
    else:
        with mock_freshdesk(rate_limit=200) as url:
            print(f"{DIM}Mock Freshdesk at {url}{RESET}")
            await run_session(url, MOCK_KEY, live=False)
            await run_bad_key(url)
        with mock_freshdesk(rate_limit=10) as url:
            await run_rate_limit(url, MOCK_KEY)

    passed = sum(call.results)
    print(f"\n{BOLD}{passed}/{len(call.results)} checks passed{RESET}")
    return 0 if passed == len(call.results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
