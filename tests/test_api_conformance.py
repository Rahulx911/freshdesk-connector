"""Behaviour pinned to the official Freshdesk API v2 docs
(https://developers.freshdesk.com/api/), checked 2026-10-01. Each test names
the doc statement it guards, because the mock alone can't prove we match the
real API."""

import httpx
import pytest

from freshdesk_connector.client import RateLimiter, request_cost
from freshdesk_connector.errors import InvalidRequest
from freshdesk_connector.query import build_ticket_query
from mock_server import data as D


# Docs, "Rate Limit": example headers are decimals, e.g. "X-Ratelimit-Total: 700.0"
def test_decimal_rate_limit_headers_are_parsed():
    rl = RateLimiter(limit_per_min=50)
    rl.observe_headers(httpx.Headers({"X-Ratelimit-Total": "700.0", "X-Ratelimit-Remaining": "426.0"}))
    assert rl.limit == 700 and rl.server_remaining == 426 and rl.budget == 560


# Docs, "List All Tickets": "Each include will consume an additional 2 credits ... stats ...
# total of 3 API credits"; "View a Ticket": "Including conversations will consume two API calls"
@pytest.mark.parametrize("params,cost", [
    ({}, 1), ({"include": "stats"}, 3), ({"include": "requester,stats"}, 5),
    ({"include": "conversations"}, 2),
])
def test_include_credit_costs(params, cost):
    assert request_cost(params) == cost


async def test_connector_counts_credits_like_server(make_service):
    svc = make_service()
    await svc.get_ticket(1, max_conversations=5)   # ticket_fields(1) + ticket w/ requester,stats(5) + convs(1)
    assert svc.c.rl.used() == 7 == len(svc.mock_state["hits"])


async def test_include_heavy_calls_respect_budget(make_service, clock):
    # Budget 8 credits/min. Two get_ticket calls cost 1 + 6 + 6 = 13 > 8,
    # so the second must wait client-side instead of drawing a 429.
    svc = make_service(server_limit=10, client_limit=10, max_wait_s=120)
    await svc.get_ticket(1, max_conversations=5)
    await svc.get_ticket(2, max_conversations=5)
    assert clock.sleeps, "expected a proactive wait"
    assert svc.mock_state["requests"] == 5      # no 429 round-trips


# Docs, "Ticket fields": status choices are account-specific ("6": ["Waiting on Customer", ...])
async def test_custom_statuses_loaded_from_ticket_fields(make_service):
    svc = make_service()
    statuses = await svc.statuses()
    assert statuses[6] == "waiting_on_customer" and statuses[2] == "open"
    res = await svc.search_tickets(status=["waiting_on_customer"])
    assert res["count"] > 0 and all(t["status"] == "waiting_on_customer" for t in res["items"])


async def test_status_fallback_when_fields_forbidden(make_service):
    svc = make_service()
    svc.mock_state["deny_ticket_fields"] = True
    t = await svc.get_ticket(7, include_conversations=False)     # ticket 7 has custom status 6
    assert t["status"] == "status_6"                             # unknown, but not mislabelled
    with pytest.raises(InvalidRequest) as e:
        await svc.search_tickets(status=["waiting_on_customer"])
    assert "open" in e.value.message                             # tells the model what IS valid


# Docs, "Filter Tickets": ':>' is "greater than or equal to", ':<' "less than or equal to"
async def test_date_bounds_are_inclusive(make_service):
    day = D.TICKETS[10]["created_at"][:10]
    res = await make_service().search_tickets(created_after=day, created_before=day)
    assert 10 + 1 in {t["id"] for t in res["items"]}


# Docs, "Filter Tickets": "To filter for fields with no values assigned, use the null keyword"
async def test_unassigned_filter(make_service):
    res = await make_service().search_tickets(unassigned=True)
    assert res["count"] > 0 and all("responder_id" not in t for t in res["items"])
    assert 'agent_id:null' in res["query"]
    with pytest.raises(InvalidRequest):
        build_ticket_query(unassigned=True, agent_id=5)


# Docs, "Filter Tickets": query "can have up to 512 characters"; page "should not exceed 10"
def test_query_length_cap():
    with pytest.raises(InvalidRequest):
        build_ticket_query(tags=[f"tag{i:03d}" for i in range(60)])


# Docs, "List All Contacts": filter by phone/mobile is an exact value match
@pytest.mark.parametrize("query,name", [
    ("+91 90000 00005", "Ishita Rao"),     # exact stored format
    ("+91-90000-00005", "Ishita Rao"),     # different punctuation -> canonical +91 variant
    ("+91 98765 43210", "Divya Menon"),    # stored as bare 10 digits
    ("09876543210", "Divya Menon"),        # trunk prefix
])
async def test_phone_lookup_variants(make_service, query, name):
    res = await make_service().find_contacts(phone=query)
    assert res["items"] and res["items"][0]["name"] == name


# Docs, "List All Tickets": "only tickets that have been created within the past 30 days"
async def test_thirty_day_default_window(make_service):
    svc = make_service()
    recent = await svc.list_tickets(per_page=100)
    everything = await svc.list_tickets(per_page=100, updated_since="2000-01-01T00:00:00Z")
    assert recent["count"] < everything["count"]
