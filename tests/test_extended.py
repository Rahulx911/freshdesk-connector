"""Beyond unit behaviour: concurrency, secret hygiene, real wall-clock rate
limiting, and the streamable-HTTP MCP transport (how a hosted Agent Studio
deployment would call the connector)."""

import asyncio
import json
import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from freshdesk_connector import mcp_server
from freshdesk_connector.errors import InvalidRequest, RateLimited

ROOT = Path(__file__).resolve().parent.parent


async def test_concurrent_calls_share_one_budget(make_service, clock):
    # 20 parallel calls against a 10/min account, connector budget 8:
    # the lock must serialise budget checks so the server never sees >10 in a window.
    svc = make_service(server_limit=10, client_limit=10, max_wait_s=600)
    results = await asyncio.gather(*[svc.get_company(501) for _ in range(20)])
    assert len(results) == 20 and all(r["id"] == 501 for r in results)
    assert svc.mock_state["requests"] == 20      # zero 429s


async def test_api_key_never_logged_or_returned(make_service, caplog):
    key = "mock-api-key-123"
    svc = make_service()
    mcp_server.set_service(svc)
    try:
        with caplog.at_level(logging.DEBUG):
            outputs = [
                await mcp_server.mcp.call_tool("connector_status", {}),
                await mcp_server.mcp.call_tool("get_ticket", {"ticket_id": 1}),
                await mcp_server.mcp.call_tool("customer_ticket_history", {"email": "asha.verma@example.com"}),
            ]
            with pytest.raises(ToolError):
                await mcp_server.mcp.call_tool("get_ticket", {"ticket_id": 999999})
    finally:
        mcp_server.set_service(None)
    blob = json.dumps(outputs, default=str) + caplog.text + repr(svc.c.creds)
    assert key not in blob


async def test_injection_and_garbage_inputs_never_reach_freshdesk(make_service):
    svc = make_service()
    before = svc.mock_state["requests"]
    for bad in ["x' OR status:5", "a\" AND priority:4", "<script>", "tag)OR(1", "' OR ''='"]:
        with pytest.raises(InvalidRequest):
            await svc.search_tickets(tags=[bad])
    assert svc.mock_state["requests"] - before <= 1   # at most the one-off ticket_fields load


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_port(port, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"port {port} never opened")


@pytest.fixture
def live_mock():
    procs = []

    def _start(rate_limit=200):
        port = _free_port()
        p = subprocess.Popen([sys.executable, "-m", "uvicorn", "mock_server.app:app", "--port", str(port),
                              "--log-level", "warning"], cwd=ROOT,
                             env={**os.environ, "MOCK_RATE_LIMIT": str(rate_limit)})
        procs.append(p)
        _wait_port(port)
        return f"http://127.0.0.1:{port}"

    yield _start
    for p in procs:
        p.terminate()
        p.wait(timeout=5)


@pytest.mark.slow
async def test_real_clock_rate_limit_fail_fast(live_mock):
    """Real HTTP, real time: account allows 6 credits/min, connector keeps 20%
    back (budget 4 once learned), and won't block a call longer than 2s."""
    from freshdesk_connector.auth import Credentials
    from freshdesk_connector.client import FreshdeskClient
    from freshdesk_connector.service import FreshdeskService

    url = live_mock(rate_limit=6)
    client = FreshdeskClient(Credentials(base_url=url, api_key="mock-api-key-123"), max_wait_s=2)
    svc = FreshdeskService(client)
    ok, t0 = 0, time.monotonic()
    with pytest.raises(RateLimited) as e:
        for _ in range(10):
            await svc.get_company(501)
            ok += 1
    elapsed = time.monotonic() - t0
    await client.aclose()
    assert ok == 4 and elapsed < 5
    assert e.value.retry_after > 2


@pytest.mark.slow
async def test_streamable_http_transport(live_mock):
    from mcp import ClientSession
    from mcp.client import streamable_http as sh
    streamablehttp_client = getattr(sh, "streamable_http_client", None) or sh.streamablehttp_client

    url = live_mock()
    port = _free_port()
    server = subprocess.Popen(
        [sys.executable, "-m", "freshdesk_connector.cli", "serve", "--transport", "streamable-http",
         "--port", str(port)], cwd=ROOT,
        env={**os.environ, "FRESHDESK_DOMAIN": url, "FRESHDESK_API_KEY": "mock-api-key-123"},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        _wait_port(port)
        async with streamablehttp_client(f"http://127.0.0.1:{port}/mcp") as (r, w, _), ClientSession(r, w) as s:
            await s.initialize()
            tools = await s.list_tools()
            assert len(tools.tools) == 10
            res = await s.call_tool("search_tickets", {"status": ["open"], "priority": ["urgent"]})
            assert not res.isError and "total_matches" in res.content[0].text
            res = await s.call_tool("get_ticket", {"ticket_id": 123456})
            assert res.isError and "not_found" in res.content[0].text
    finally:
        server.terminate()
        server.wait(timeout=5)
