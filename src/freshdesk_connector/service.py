"""Read-only primitives over Freshdesk: list / get / search.

Each method returns plain dicts shaped for an LLM: normalized records plus
explicit pagination (`page`, `has_more`, `next_page`) so the agent knows
whether it has the full picture."""

from __future__ import annotations

from typing import Any

from .client import FreshdeskClient
from .errors import InvalidRequest, NotFound
from .normalize import Normalizer
from .query import build_ticket_query, iso_or_none

SEARCH_PAGE_SIZE = 30   # fixed by Freshdesk for /search
SEARCH_MAX_PAGE = 10    # Freshdesk caps search at 10 pages (300 results)
LIST_MAX_PER_PAGE = 100
ORDER_FIELDS = {"created_at", "updated_at", "due_by", "status"}


def _page_args(page: int, per_page: int) -> tuple[int, int]:
    if page < 1:
        raise InvalidRequest("page must be >= 1")
    if not 1 <= per_page <= LIST_MAX_PER_PAGE:
        raise InvalidRequest(f"per_page must be between 1 and {LIST_MAX_PER_PAGE}")
    return page, per_page


def _paged(items: list, page: int, has_more: bool, **extra: Any) -> dict:
    out = {"items": items, "count": len(items), "page": page, "has_more": has_more}
    if has_more:
        out["next_page"] = page + 1
    out.update(extra)
    return out


class FreshdeskService:
    def __init__(self, client: FreshdeskClient, normalizer: Normalizer | None = None):
        self.c = client
        self.n = normalizer or Normalizer()

    # ---------------------------------------------------------------- tickets
    async def list_tickets(
        self,
        *,
        updated_since: str | None = None,
        requester_email: str | None = None,
        requester_id: int | None = None,
        company_id: int | None = None,
        order_by: str = "updated_at",
        order: str = "desc",
        page: int = 1,
        per_page: int = 30,
    ) -> dict:
        page, per_page = _page_args(page, per_page)
        if order_by not in ORDER_FIELDS:
            raise InvalidRequest(f"order_by must be one of {sorted(ORDER_FIELDS)}")
        if order not in ("asc", "desc"):
            raise InvalidRequest("order must be 'asc' or 'desc'")
        params = {
            "updated_since": iso_or_none(updated_since),
            "email": requester_email,
            "requester_id": requester_id,
            "company_id": company_id,
            "order_by": order_by,
            "order_type": order,
            "page": page,
            "per_page": per_page,
        }
        data, has_next = await self.c.get_page("/api/v2/tickets", params)
        note = None
        if not updated_since:
            note = ("Freshdesk only lists tickets created in the last 30 days unless "
                    "updated_since is given.")
        extra = {"note": note} if note else {}
        return _paged([self.n.ticket(t) for t in data], page, has_next, **extra)

    async def search_tickets(self, *, page: int = 1, **filters: Any) -> dict:
        if not 1 <= page <= SEARCH_MAX_PAGE:
            raise InvalidRequest(f"search page must be between 1 and {SEARCH_MAX_PAGE}")
        query = build_ticket_query(**filters)
        data = await self.c.get_json("/api/v2/search/tickets", {"query": query, "page": page})
        total = int(data.get("total", 0))
        results = data.get("results", [])
        has_more = page * SEARCH_PAGE_SIZE < total and page < SEARCH_MAX_PAGE
        extra: dict[str, Any] = {"total_matches": total, "query": query}
        if total > SEARCH_PAGE_SIZE * SEARCH_MAX_PAGE:
            extra["warning"] = (f"{total} matches but Freshdesk search returns at most "
                                f"{SEARCH_PAGE_SIZE * SEARCH_MAX_PAGE}; narrow the filters "
                                "(e.g. a date range) to see everything.")
        return _paged([self.n.ticket(t) for t in results], page, has_more, **extra)

    async def get_ticket(
        self, ticket_id: int, *, include_conversations: bool = True, max_conversations: int = 20
    ) -> dict:
        t = await self.c.get_json(f"/api/v2/tickets/{int(ticket_id)}",
                                  {"include": "requester,stats"})
        out = self.n.ticket(t, include_body=True)
        if include_conversations:
            convs = await self.list_ticket_conversations(
                ticket_id, page=1, per_page=min(max(max_conversations, 1), 100)
            )
            out["conversations"] = convs["items"]
            out["conversations_truncated"] = convs["has_more"]
        return out

    async def list_ticket_conversations(self, ticket_id: int, *, page: int = 1, per_page: int = 30) -> dict:
        page, per_page = _page_args(page, per_page)
        data, has_next = await self.c.get_page(
            f"/api/v2/tickets/{int(ticket_id)}/conversations", {"page": page, "per_page": per_page}
        )
        return _paged([self.n.conversation(c) for c in data], page, has_next, ticket_id=ticket_id)

    # --------------------------------------------------------------- contacts
    async def get_contact(self, contact_id: int) -> dict:
        return self.n.contact(await self.c.get_json(f"/api/v2/contacts/{int(contact_id)}"))

    async def find_contacts(
        self, *, email: str | None = None, phone: str | None = None, name: str | None = None,
        limit: int = 10,
    ) -> dict:
        if sum(x is not None for x in (email, phone, name)) != 1:
            raise InvalidRequest("Provide exactly one of email, phone, name")
        if email:
            data = await self.c.get_json("/api/v2/contacts", {"email": email.strip().lower()})
        elif phone:
            digits = "".join(ch for ch in phone if ch.isdigit() or ch == "+")
            data = await self.c.get_json("/api/v2/contacts", {"phone": digits})
            if not data:
                data = await self.c.get_json("/api/v2/contacts", {"mobile": digits})
        else:
            if len(name.strip()) < 2:
                raise InvalidRequest("name must be at least 2 characters")
            hits = await self.c.get_json("/api/v2/contacts/autocomplete", {"term": name.strip()})
            data = []
            for h in hits[:limit]:
                try:
                    data.append(await self.c.get_json(f"/api/v2/contacts/{int(h['id'])}"))
                except NotFound:
                    continue
        items = [self.n.contact(c) for c in data[:limit]]
        return {"items": items, "count": len(items)}

    async def customer_ticket_history(self, *, email: str, limit: int = 10) -> dict:
        """Convenience composite: who is this customer + their recent tickets.
        Saves the agent two round trips for the most common support question."""
        contacts = await self.find_contacts(email=email, limit=1)
        if not contacts["items"]:
            return {"contact": None, "tickets": [], "note": f"No contact with email {email}"}
        contact = contacts["items"][0]
        tickets = await self.list_tickets(
            requester_id=contact["id"], updated_since="2000-01-01T00:00:00Z",
            per_page=min(max(limit, 1), 100),
        )
        counts: dict[str, int] = {}
        for t in tickets["items"]:
            counts[t.get("status", "unknown")] = counts.get(t.get("status", "unknown"), 0) + 1
        return {"contact": contact, "tickets": tickets["items"], "status_counts": counts,
                "has_more": tickets["has_more"]}

    # -------------------------------------------------------------- companies
    async def get_company(self, company_id: int) -> dict:
        return self.n.company(await self.c.get_json(f"/api/v2/companies/{int(company_id)}"))

    async def find_companies(self, *, name: str, limit: int = 10) -> dict:
        if len(name.strip()) < 2:
            raise InvalidRequest("name must be at least 2 characters")
        data = await self.c.get_json("/api/v2/companies/autocomplete", {"name": name.strip()})
        comps = data.get("companies", data) if isinstance(data, dict) else data
        items = [{"id": c.get("id"), "name": c.get("name")} for c in comps[:limit]]
        return {"items": items, "count": len(items)}

    # ------------------------------------------------------------------- meta
    async def connector_status(self) -> dict:
        me = await self.c.whoami()
        contact = me.get("contact", {}) if isinstance(me, dict) else {}
        return {
            "connected": True,
            "freshdesk_url": self.c.creds.base_url,
            "authenticated_as": {"agent_id": me.get("id"), "name": contact.get("name")},
            "access": "read-only",
            "pii_redaction": self.n.redact_pii,
            "rate_limit": self.c.rl.snapshot(),
        }
