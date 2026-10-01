"""MCP server exposing the Freshdesk connector to an Agent Studio agent.

All tools are read-only (MCP `readOnlyHint`), return normalized JSON, and
surface failures as MCP tool errors whose text is a JSON object with an
`error` code and a `hint` telling the model what to do next."""

from __future__ import annotations

import json
import logging
import os
from typing import Annotated, Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from .auth import resolve_credentials
from .client import FreshdeskClient, RateLimiter
from .errors import FreshdeskError
from .normalize import Normalizer
from .service import FreshdeskService

log = logging.getLogger("freshdesk_connector")

INSTRUCTIONS = """\
Read-only access to the merchant's Freshdesk helpdesk: tickets, ticket
conversations, contacts (customers) and companies.

How to use:
- Customer asks "what's happening with my issue?" -> customer_ticket_history(email)
  then get_ticket(id) for the one that matters.
- Filtering by status/priority/tag/date -> search_tickets (structured filters, max 300 results).
- Browsing recent activity -> list_tickets(updated_since=...).
- Statuses: open, pending, resolved, closed, plus the account's custom statuses
  (connector_status lists them).
- Priorities: low, medium, high, urgent.

Limits: cannot create, reply to, update or delete anything. Cannot full-text
search ticket bodies. If a tool returns error=rate_limited, wait
retry_after_seconds before calling ANY Freshdesk tool again. Private notes are
internal agent notes (private_note=true): never quote them to the end customer.
"""

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True,
                            openWorldHint=True)

mcp = FastMCP("freshdesk", instructions=INSTRUCTIONS)

_service: FreshdeskService | None = None


def get_service() -> FreshdeskService:
    global _service
    if _service is None:
        creds = resolve_credentials()
        client = FreshdeskClient(
            creds,
            rate_limiter=RateLimiter(
                reserve_fraction=float(os.environ.get("FRESHDESK_RATE_RESERVE", "0.2"))
            ),
            max_wait_s=float(os.environ.get("FRESHDESK_MAX_WAIT_S", "20")),
        )
        normalizer = Normalizer(
            redact_pii=os.environ.get("FRESHDESK_REDACT_PII", "false").lower() in ("1", "true", "yes"),
            max_body_chars=int(os.environ.get("FRESHDESK_MAX_BODY_CHARS", "2000")),
        )
        _service = FreshdeskService(client, normalizer)
    return _service


def set_service(svc: FreshdeskService | None) -> None:
    """Test hook."""
    global _service
    _service = svc


async def _call(coro_fn, *args, **kwargs):
    try:
        return await coro_fn(*args, **kwargs)
    except FreshdeskError as e:
        log.warning("tool error: %s", e.code)
        raise ToolError(json.dumps(e.to_dict())) from e


Priority = Literal["low", "medium", "high", "urgent"]


@mcp.tool(annotations=READ_ONLY)
async def list_tickets(
    updated_since: Annotated[str | None, Field(description="ISO-8601 timestamp; only tickets updated after it. Without it Freshdesk returns only tickets created in the last 30 days.")] = None,
    requester_email: Annotated[str | None, Field(description="Only tickets raised by this email")] = None,
    company_id: Annotated[int | None, Field(description="Only tickets for this company id")] = None,
    order_by: Literal["created_at", "updated_at", "due_by", "status"] = "updated_at",
    order: Literal["asc", "desc"] = "desc",
    page: Annotated[int, Field(ge=1)] = 1,
    per_page: Annotated[int, Field(ge=1, le=100)] = 30,
) -> dict:
    """List tickets, newest activity first. Use for browsing recent tickets or a
    customer's/company's tickets. For status/priority/tag filters use search_tickets."""
    svc = get_service()
    return await _call(svc.list_tickets, updated_since=updated_since, requester_email=requester_email,
                       company_id=company_id, order_by=order_by, order=order, page=page,
                       per_page=per_page)


@mcp.tool(annotations=READ_ONLY)
async def search_tickets(
    status: Annotated[list[str] | None, Field(description="Match any of these statuses: open, pending, resolved, closed, plus any custom statuses the account defines (e.g. waiting_on_customer); connector_status lists them")] = None,
    priority: Annotated[list[Priority] | None, Field(description="Match any of these priorities")] = None,
    tags: Annotated[list[str] | None, Field(description="Match any of these tags")] = None,
    ticket_type: Annotated[str | None, Field(description="Ticket type, e.g. 'Refund', 'Question'")] = None,
    agent_id: Annotated[int | None, Field(description="Assigned agent id")] = None,
    unassigned: Annotated[bool, Field(description="Only tickets with no agent assigned")] = False,
    group_id: Annotated[int | None, Field(description="Assigned group id")] = None,
    created_after: Annotated[str | None, Field(description="YYYY-MM-DD, inclusive (on or after)")] = None,
    created_before: Annotated[str | None, Field(description="YYYY-MM-DD, inclusive (on or before)")] = None,
    updated_after: Annotated[str | None, Field(description="YYYY-MM-DD, inclusive")] = None,
    updated_before: Annotated[str | None, Field(description="YYYY-MM-DD, inclusive")] = None,
    due_before: Annotated[str | None, Field(description="YYYY-MM-DD, inclusive")] = None,
    page: Annotated[int, Field(ge=1, le=10, description="30 results per page, max 10 pages")] = 1,
) -> dict:
    """Search tickets by structured filters (AND across fields, OR within a list).
    Returns total_matches. Does NOT search subject/description text, skips archived
    tickets, and very recent changes can take a few minutes to become searchable."""
    svc = get_service()
    return await _call(svc.search_tickets, status=status, priority=priority, tags=tags,
                       ticket_type=ticket_type, agent_id=agent_id, unassigned=unassigned,
                       group_id=group_id,
                       created_after=created_after, created_before=created_before,
                       updated_after=updated_after, updated_before=updated_before,
                       due_before=due_before, page=page)


@mcp.tool(annotations=READ_ONLY)
async def get_ticket(
    ticket_id: Annotated[int, Field(ge=1)],
    include_conversations: bool = True,
    max_conversations: Annotated[int, Field(ge=1, le=100)] = 20,
) -> dict:
    """Get one ticket with description, requester, SLA stats and its conversation
    thread (customer replies, agent replies and private notes)."""
    svc = get_service()
    return await _call(svc.get_ticket, ticket_id, include_conversations=include_conversations,
                       max_conversations=max_conversations)


@mcp.tool(annotations=READ_ONLY)
async def list_ticket_conversations(
    ticket_id: Annotated[int, Field(ge=1)],
    page: Annotated[int, Field(ge=1)] = 1,
    per_page: Annotated[int, Field(ge=1, le=100)] = 30,
) -> dict:
    """Page through a long ticket thread oldest-first (use after get_ticket reports
    conversations_truncated=true)."""
    svc = get_service()
    return await _call(svc.list_ticket_conversations, ticket_id, page=page, per_page=per_page)


@mcp.tool(annotations=READ_ONLY)
async def find_contacts(
    email: Annotated[str | None, Field(description="Exact email")] = None,
    phone: Annotated[str | None, Field(description="Phone or mobile number")] = None,
    name: Annotated[str | None, Field(description="Name prefix (autocomplete)")] = None,
    limit: Annotated[int, Field(ge=1, le=25)] = 10,
) -> dict:
    """Find customers (contacts) by exactly one of email, phone, or name."""
    svc = get_service()
    return await _call(svc.find_contacts, email=email, phone=phone, name=name, limit=limit)


@mcp.tool(annotations=READ_ONLY)
async def get_contact(contact_id: Annotated[int, Field(ge=1)]) -> dict:
    """Get one customer (contact) by id."""
    return await _call(get_service().get_contact, contact_id)


@mcp.tool(annotations=READ_ONLY)
async def customer_ticket_history(
    email: Annotated[str, Field(description="Customer email")],
    limit: Annotated[int, Field(ge=1, le=100)] = 10,
) -> dict:
    """One call for 'who is this customer and what have they raised?': the contact
    record plus their most recently updated tickets and a count by status."""
    return await _call(get_service().customer_ticket_history, email=email, limit=limit)


@mcp.tool(annotations=READ_ONLY)
async def get_company(company_id: Annotated[int, Field(ge=1)]) -> dict:
    """Get one company (B2B account) by id."""
    return await _call(get_service().get_company, company_id)


@mcp.tool(annotations=READ_ONLY)
async def find_companies(
    name: Annotated[str, Field(description="Company name prefix")],
    limit: Annotated[int, Field(ge=1, le=25)] = 10,
) -> dict:
    """Find companies by name prefix. Returns ids and names; use get_company for details."""
    return await _call(get_service().find_companies, name=name, limit=limit)


@mcp.tool(annotations=READ_ONLY)
async def connector_status() -> dict:
    """Check the connection: which Freshdesk account and agent identity is in use,
    access level, PII redaction, and current rate-limit headroom."""
    return await _call(get_service().connector_status)


def _normalise_descriptions() -> None:
    """Python 3.13+ strips docstring indentation at compile time; older versions don't.
    Clean them here so every Python serves the model byte-identical tool text."""
    import inspect
    for tool in mcp._tool_manager.list_tools():
        if tool.description:
            tool.description = inspect.cleandoc(tool.description)


_normalise_descriptions()


def run(transport: str = "stdio") -> None:
    mcp.run(transport=transport)  # type: ignore[arg-type]
