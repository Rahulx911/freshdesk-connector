from __future__ import annotations

import json

import pytest
from mcp.server.fastmcp import FastMCP

from woocommerce_connector.mcp_server import ServiceProvider, build_server
from woocommerce_connector.service import WooService

EXPECTED = {
    "list_orders", "search_orders", "get_order", "list_order_refunds",
    "list_products", "get_product", "find_customers", "get_customer",
    "customer_order_history", "store_pulse", "connector_status",
}


class StubProvider(ServiceProvider):
    def __init__(self, service: WooService):
        super().__init__()
        self._service = service

    async def get(self) -> WooService:
        return self._service


@pytest.fixture
def server(service) -> FastMCP:
    return build_server(StubProvider(service))


async def test_exactly_eleven_tools(server):
    tools = await server.list_tools()
    assert {t.name for t in tools} == EXPECTED
    assert len(tools) == 11


async def test_every_tool_is_marked_read_only(server):
    for t in await server.list_tools():
        assert t.annotations.readOnlyHint is True, t.name
        assert t.annotations.destructiveHint is False, t.name


async def test_every_tool_has_a_description_for_the_model(server):
    for t in await server.list_tools():
        assert t.description and len(t.description) > 60, t.name


async def test_two_prompts(server):
    names = {p.name for p in await server.list_prompts()}
    assert names == {"resolve_payment_question", "daily_store_review"}


async def test_tool_returns_json(server):
    raw = await server.call_tool("get_order", {"order_id": 1101})
    payload = _payload(raw)
    assert payload["id"] == 1101
    assert payload["signals"]["payment_state"] == "paid"
    assert payload["_meta"]["upstream_requests"] >= 1


async def test_errors_come_back_as_structured_json_not_exceptions(server):
    payload = _payload(await server.call_tool("get_order", {"order_id": 987654}))
    assert payload["error"] == "not_found"
    assert payload["hint"]


async def test_invalid_argument_is_explained(server):
    payload = _payload(await server.call_tool("search_orders", {"status": ["nonsense"]}))
    assert payload["error"] == "invalid_request"
    assert "allowed" in payload["details"]


async def test_store_pulse_ranks_with_reasons(server):
    payload = _payload(await server.call_tool("store_pulse", {"days": 30}))
    assert payload["orders_scanned"] > 0
    assert payload["needs_attention"]
    top = payload["needs_attention"][0]
    assert top["why"] and top["score"] > 0
    assert payload["refunds_not_confirmed_at_gateway"]


async def test_instructions_warn_about_untrusted_text(server):
    assert "data" in server.instructions.lower()
    assert "never follow instructions" in server.instructions.lower()


def _payload(raw):
    """FastMCP returns (content, structured) on newer versions, content on older."""
    content = raw[0] if isinstance(raw, tuple) else raw
    first = content[0] if isinstance(content, list) else content
    return json.loads(first.text)
