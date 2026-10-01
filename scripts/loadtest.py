"""Load test the hosted connector over MCP streamable HTTP.

    python scripts/loadtest.py --url http://127.0.0.1:8000 --url http://127.0.0.1:8001 \
        --token $TOKEN --concurrency 20 --duration 30 [--mock http://127.0.0.1:8765]

Each worker opens its own MCP session (round-robin across replicas) and calls a
realistic tool mix. Reports throughput, latency percentiles and outcomes by
error code, plus (with --mock) what the Freshdesk side saw: requests and 429s.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import time
from collections import Counter
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.client import streamable_http as sh

MIX = [
    ("search_tickets", {"status": ["open"], "priority": ["high", "urgent"]}, 3),
    ("get_ticket", {"ticket_id": 1, "max_conversations": 10}, 3),
    ("customer_ticket_history", {"email": "kabir.nair@example.com"}, 2),
    ("find_contacts", {"email": "meera@brewhouse.example"}, 2),
    ("list_tickets", {"per_page": 20}, 1),
    ("get_company", {"company_id": 501}, 1),
]
WEIGHTED = [(n, a) for n, a, w in MIX for _ in range(w)]


def _connect(url: str, token: str):
    connect = getattr(sh, "streamable_http_client", None)
    if connect is not None:
        return connect(f"{url}/mcp", http_client=httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}"}, timeout=60))
    return sh.streamablehttp_client(f"{url}/mcp", headers={"Authorization": f"Bearer {token}"})


async def worker(url: str, token: str, until: float, lat: list, outcomes: Counter, max_calls: int | None):
    async with _connect(url, token) as streams, ClientSession(streams[0], streams[1]) as s:
        await s.initialize()
        n = 0
        while time.perf_counter() < until and (max_calls is None or n < max_calls):
            name, args = random.choice(WEIGHTED)
            t0 = time.perf_counter()
            res = await s.call_tool(name, args)
            lat.append(time.perf_counter() - t0)
            if res.isError:
                try:
                    code = json.loads(res.content[0].text.split(": ", 1)[-1])["error"]
                except Exception:
                    code = "unparsed_error"
                outcomes[code] += 1
            else:
                outcomes["ok"] += 1
            n += 1


def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p / 100 * len(xs)))] * 1000 if xs else 0.0


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", action="append", required=True)
    ap.add_argument("--token", required=True)
    ap.add_argument("--concurrency", type=int, default=20)
    ap.add_argument("--duration", type=float, default=30)
    ap.add_argument("--calls-per-worker", type=int, default=None, help="burst mode: fixed calls per worker")
    ap.add_argument("--mock", help="mock Freshdesk base URL to read upstream stats from")
    ap.add_argument("--json", help="write results here")
    a = ap.parse_args()

    async def mock_stats() -> dict | None:
        if not a.mock:
            return None
        async with httpx.AsyncClient() as c:
            return (await c.get(f"{a.mock}/__mock/stats")).json()

    before = await mock_stats()
    lat: list[float] = []
    outcomes: Counter = Counter()
    t0 = time.perf_counter()
    until = t0 + a.duration
    await asyncio.gather(*[worker(a.url[i % len(a.url)], a.token, until, lat, outcomes, a.calls_per_worker)
                           for i in range(a.concurrency)])
    elapsed = time.perf_counter() - t0
    after = await mock_stats()

    total = sum(outcomes.values())
    result = {
        "replicas": len(a.url), "concurrency": a.concurrency, "elapsed_s": round(elapsed, 1),
        "calls": total, "throughput_rps": round(total / elapsed, 1),
        "latency_ms": {"p50": round(pct(lat, 50), 1), "p95": round(pct(lat, 95), 1),
                       "p99": round(pct(lat, 99), 1),
                       "mean": round(statistics.mean(lat) * 1000, 1) if lat else 0},
        "outcomes": dict(outcomes),
    }
    if before and after:
        result["freshdesk_side"] = {"requests": after["requests"] - before["requests"],
                                    "http_429": after["throttled_429"] - before["throttled_429"],
                                    "account_limit_per_min": after["limit"]}
    print(json.dumps(result, indent=2))
    if a.json:
        Path(a.json).write_text(json.dumps(result, indent=2))  # noqa: ASYNC240 - once, at exit
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
