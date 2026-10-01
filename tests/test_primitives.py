import pytest

from freshdesk_connector.errors import InvalidRequest, NotFound
from freshdesk_connector.query import build_ticket_query


# ------------------------------------------------------------------ list
async def test_list_default_window_has_note(make_service):
    res = await make_service().list_tickets()
    assert "note" in res and "30 days" in res["note"]
    assert res["items"] and all("description" not in t for t in res["items"])
    assert res["items"][0]["status"] in {"open", "pending", "resolved", "closed", "waiting_on_customer"}


async def test_list_pagination(make_service):
    svc = make_service()
    p1 = await svc.list_tickets(updated_since="2000-01-01T00:00:00Z", per_page=10)
    assert p1["count"] == 10 and p1["has_more"] and p1["next_page"] == 2
    p4 = await svc.list_tickets(updated_since="2000-01-01T00:00:00Z", per_page=10, page=4)
    from mock_server.data import TICKETS
    assert p4["count"] == len(TICKETS) - 30 and not p4["has_more"]
    ids = {t["id"] for t in p1["items"]} & {t["id"] for t in p4["items"]}
    assert not ids


async def test_list_by_requester_email(make_service):
    res = await make_service().list_tickets(updated_since="2000-01-01T00:00:00Z",
                                            requester_email="asha.verma@example.com")
    assert res["items"] and all(t["requester_id"] == 1000 for t in res["items"])


@pytest.mark.parametrize("kw", [{"per_page": 101}, {"page": 0}, {"order_by": "subject"},
                                {"updated_since": "yesterday"}])
async def test_list_validation(make_service, kw):
    with pytest.raises(InvalidRequest):
        await make_service().list_tickets(**kw)


# ---------------------------------------------------------------- search
def test_query_builder():
    q = build_ticket_query(status=["open", "pending"], priority=["urgent"], tags=["refund"],
                           created_after="2026-01-01")
    assert q == "\"(status:2 OR status:3) AND priority:4 AND tag:'refund' AND created_at:>'2026-01-01'\""


@pytest.mark.parametrize("kw", [
    {"tags": ["x' OR status:5"]},          # injection attempt
    {"status": ["stuck"]},
    {"created_after": "last week"},
    {},
])
def test_query_builder_rejects(kw):
    with pytest.raises(InvalidRequest):
        build_ticket_query(**kw)


async def test_search_by_status_and_priority(make_service):
    res = await make_service().search_tickets(status=["open"], priority=["high", "urgent"])
    assert res["total_matches"] == res["count"] > 0
    assert all(t["status"] == "open" and t["priority"] in ("high", "urgent") for t in res["items"])


async def test_search_by_tag_and_dates(make_service):
    res = await make_service().search_tickets(tags=["payments"], created_after="2000-01-01")
    assert res["count"] > 0 and all("payments" in t["tags"] for t in res["items"])


async def test_search_page_bounds(make_service):
    with pytest.raises(InvalidRequest):
        await make_service().search_tickets(status=["open"], page=11)


# ------------------------------------------------------------------- get
async def test_get_ticket_full(make_service):
    t = await make_service().get_ticket(1)
    assert t["id"] == 1 and "<p>" not in t["description"] and "KL-10200" in t["description"]
    assert t["requester"]["email"] == "asha.verma@example.com"
    assert t["conversations"][0]["from"] == "agent"
    # ticket 1 has an internal note: withheld by default (customer-facing agents) ...
    assert not any(c["private_note"] for c in t["conversations"])
    assert t["private_notes_withheld"] == 1


async def test_private_notes_opt_in(make_service):
    t = await make_service(private_notes=True).get_ticket(1)
    assert any(c["private_note"] for c in t["conversations"]) and "private_notes_withheld" not in t


async def test_get_ticket_long_thread_truncated(make_service):
    svc = make_service()
    t = await svc.get_ticket(5, max_conversations=10)
    assert len(t["conversations"]) == 10 and t["conversations_truncated"] is True
    p2 = await svc.list_ticket_conversations(5, page=2, per_page=10)
    assert p2["items"][0]["id"] != t["conversations"][0]["id"]


async def test_get_ticket_not_found(make_service):
    with pytest.raises(NotFound) as e:
        await make_service().get_ticket(99999)
    assert "search" in e.value.hint


# -------------------------------------------------------------- contacts
async def test_find_contacts(make_service):
    svc = make_service()
    assert (await svc.find_contacts(email="MEERA@brewhouse.example"))["items"][0]["id"] == 1002
    assert (await svc.find_contacts(phone="+91-90000-00005"))["items"][0]["name"] == "Ishita Rao"
    by_name = await svc.find_contacts(name="Kab")
    assert [c["name"] for c in by_name["items"]] == ["Kabir Nair"]
    with pytest.raises(InvalidRequest):
        await svc.find_contacts(email="a@b.c", name="x")


async def test_customer_ticket_history(make_service):
    res = await make_service().customer_ticket_history(email="kabir.nair@example.com")
    assert res["contact"]["name"] == "Kabir Nair"
    assert res["tickets"] and sum(res["status_counts"].values()) == len(res["tickets"])
    missing = await make_service().customer_ticket_history(email="nobody@example.com")
    assert missing["contact"] is None


async def test_companies(make_service):
    svc = make_service()
    hits = await svc.find_companies(name="Br")
    assert {c["name"] for c in hits["items"]} == {"Brewhouse Cafes", "Brightlane Offices"}
    comp = await svc.get_company(501)
    assert comp["health_score"] == "At risk"


async def test_pii_redaction(make_service):
    svc = make_service(redact=True, private_notes=True)
    t = await svc.get_ticket(1)
    assert t["requester"]["email"].startswith("a***@")
    note = next(c for c in t["conversations"] if c["private_note"])
    assert "90000 11111" not in note["body"] and "***111" in note["body"]
