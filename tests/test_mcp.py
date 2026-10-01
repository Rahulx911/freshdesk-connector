"""MCP surface: tool catalogue, schemas, annotations, and error shape."""

import json

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from freshdesk_connector import mcp_server

EXPECTED = {
    "list_tickets", "search_tickets", "get_ticket", "list_ticket_conversations",
    "find_contacts", "get_contact", "customer_ticket_history", "get_company",
    "find_companies", "connector_status",
}


@pytest.fixture
def wired(make_service):
    svc = make_service()
    mcp_server.set_service(svc)
    yield svc
    mcp_server.set_service(None)


async def test_tool_catalogue_is_read_only():
    tools = await mcp_server.mcp.list_tools()
    assert {t.name for t in tools} == EXPECTED
    for t in tools:
        assert t.annotations.readOnlyHint is True and t.annotations.destructiveHint is False
        assert t.description and t.inputSchema["type"] == "object"


async def test_search_schema_exposes_enums():
    tools = {t.name: t for t in await mcp_server.mcp.list_tools()}
    schema = json.dumps(tools["search_tickets"].inputSchema)
    for word in ("waiting_on_customer", "urgent"):
        assert word in schema


async def test_call_tool_roundtrip(wired):
    result = await mcp_server.mcp.call_tool("search_tickets", {"status": ["open"], "priority": ["urgent"]})
    payload = result[1] if isinstance(result, tuple) else result
    text = json.dumps(payload, default=str)
    assert "total_matches" in text


async def test_tool_error_is_structured(wired):
    with pytest.raises(ToolError) as e:
        await mcp_server.mcp.call_tool("get_ticket", {"ticket_id": 424242})
    body = json.loads(str(e.value).split("Error executing tool get_ticket: ")[-1])
    assert body["error"] == "not_found" and "hint" in body
