"""Read-only primitives over Freshdesk: list / get / search.

Each method returns plain dicts shaped for an LLM: normalized records plus
explicit pagination (`page`, `has_more`, `next_page`) so the agent knows
whether it has the full picture."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from .client import FreshdeskClient
from .errors import AuthError, InvalidRequest, NotFound, PermissionDenied, RateLimited, UpstreamError
from .normalize import STATUS, Normalizer, slugify
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


def _human_hours(h: float) -> str:
    h = abs(h)
    if h >= 48:
        return f"{round(h / 24)} days"
    if h >= 1:
        return f"{round(h)}h"
    return f"{max(1, round(h * 60))} min"


class FreshdeskService:
    STATUS_TTL_S = 3600          # admins add/rename statuses; pick changes up hourly

    def __init__(self, client: FreshdeskClient, normalizer: Normalizer | None = None,
                 clock: Any = time.monotonic):
        self.c = client
        self.n = normalizer or Normalizer()
        self._clock = clock
        self._statuses_until = 0.0
        self._statuses_lock = asyncio.Lock()

    async def statuses(self) -> dict[int, str]:
        """The account's ticket statuses, including custom ones, from /api/v2/ticket_fields.

        Cached for STATUS_TTL_S; concurrent first calls share one fetch. If the key may
        not read ticket fields (403/404) we settle on the built-in 2-5 for the TTL. A
        *transient* failure (rate limit, Freshdesk down) is not cached: this call uses
        whatever we already know and the next call retries. Auth failures propagate."""
        if self._clock() < self._statuses_until:
            return self.n.status_names
        async with self._statuses_lock:
            if self._clock() < self._statuses_until:          # another task just refreshed
                return self.n.status_names
            try:
                fields = await self.c.get_json("/api/v2/ticket_fields")
            except AuthError:
                raise
            except (PermissionDenied, NotFound):
                self.n.status_names = dict(STATUS)
                self._statuses_until = self._clock() + self.STATUS_TTL_S
                return self.n.status_names
            except (RateLimited, UpstreamError):
                return self.n.status_names
            for f in fields if isinstance(fields, list) else []:
                if f.get("type") == "default_status" and isinstance(f.get("choices"), dict):
                    names: dict[int, str] = {}
                    for sid, labels in f["choices"].items():
                        label = labels[0] if isinstance(labels, list) and labels else str(labels)
                        names[int(sid)] = slugify(label)
                    if names:
                        self.n.status_names = names
            self._statuses_until = self._clock() + self.STATUS_TTL_S
            return self.n.status_names

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
        await self.statuses()
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
        names = await self.statuses()
        query = build_ticket_query(status_ids={v: k for k, v in names.items()}, **filters)
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
        await self.statuses()
        t = await self.c.get_json(f"/api/v2/tickets/{int(ticket_id)}",
                                  {"include": "requester,stats"})
        out = self.n.ticket(t, include_body=True)
        if include_conversations:
            convs = await self.list_ticket_conversations(
                ticket_id, page=1, per_page=min(max(max_conversations, 1), 100)
            )
            out["conversations"] = convs["items"]
            out["conversations_returned"] = len(convs["items"])   # LLMs miscount long lists
            out["conversations_truncated"] = convs["has_more"]
            if convs.get("private_notes_withheld"):
                out["private_notes_withheld"] = convs["private_notes_withheld"]
            # fold references found anywhere in the visible thread into the ticket's signals
            refs = list(out.get("signals", {}).get("payment_refs", []))
            for c in convs["items"]:
                refs.extend(c.get("payment_refs", []))
            if refs:
                seen, merged = set(), []
                for r in refs:
                    if (r["type"], r["value"]) not in seen:
                        seen.add((r["type"], r["value"]))
                        merged.append(r)
                out.setdefault("signals", {})["payment_refs"] = merged
        return out

    async def list_ticket_conversations(self, ticket_id: int, *, page: int = 1, per_page: int = 30) -> dict:
        page, per_page = _page_args(page, per_page)
        data, has_next = await self.c.get_page(
            f"/api/v2/tickets/{int(ticket_id)}/conversations", {"page": page, "per_page": per_page}
        )
        kept, withheld = self.n.conversations(data)
        extra: dict[str, Any] = {"ticket_id": ticket_id}
        if withheld:
            extra["private_notes_withheld"] = withheld
        return _paged(kept, page, has_next, **extra)

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
            # Freshdesk matches phone/mobile exactly as stored, and merchants store numbers
            # inconsistently ("+91 98200 12345", "9820012345", "+919820012345"). Try the
            # common shapes (Indian formats included), stopping at the first hit.
            # Worst case costs 2 credits per variant, so the list is kept short.
            raw = phone.strip()
            digits = "".join(ch for ch in raw if ch.isdigit())
            last10 = digits[-10:]
            variants = list(dict.fromkeys(v for v in (
                raw,
                ("+" + digits) if raw.startswith("+") else digits,
                last10,
                f"+91{last10}" if len(last10) == 10 else "",
                f"+91 {last10[:5]} {last10[5:]}" if len(last10) == 10 else "",
            ) if v))
            data = []
            for v in variants:
                for field in ("phone", "mobile"):
                    data = await self.c.get_json("/api/v2/contacts", {field: v})
                    if data:
                        break
                if data:
                    break
        else:
            if name is None or len(name.strip()) < 2:
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

    # ------------------------------------------------------------------ pulse
    async def support_pulse(self, *, max_tickets: int = 90, top: int = 10) -> dict:
        """What should the support team look at right now?

        Pulls the active backlog (every status except resolved/closed, custom ones
        included) through search, then scores each ticket with explainable reasons:
        SLA overdue/due soon, urgent/high priority, escalated, payment-related (needs a
        Razorpay lookup), unassigned. Costs at most ceil(max_tickets/30) API credits."""
        if not 30 <= max_tickets <= SEARCH_PAGE_SIZE * SEARCH_MAX_PAGE:
            raise InvalidRequest(f"max_tickets must be between 30 and {SEARCH_PAGE_SIZE * SEARCH_MAX_PAGE}")
        if not 1 <= top <= 25:
            raise InvalidRequest("top must be between 1 and 25")
        names = await self.statuses()
        active = sorted(n for n in names.values() if n not in ("resolved", "closed"))
        tickets: list[dict] = []
        total = 0
        for page in range(1, -(-max_tickets // SEARCH_PAGE_SIZE) + 1):
            res = await self.search_tickets(status=active, page=page)
            total = res["total_matches"]
            tickets.extend(res["items"])
            if not res["has_more"]:
                break
        tickets = tickets[:max_tickets]

        def bump(d: dict[str, int], k: Any) -> None:
            d[str(k)] = d.get(str(k), 0) + 1

        by_intent: dict[str, int] = {}
        by_priority: dict[str, int] = {}
        by_status: dict[str, int] = {}
        scored = []
        for t in tickets:
            sig = t.get("signals", {})
            bump(by_intent, sig.get("intent", "other"))
            bump(by_priority, t.get("priority"))
            bump(by_status, t.get("status"))
            score, why = 0, []
            sla = sig.get("sla") or {}
            overdue_h = 0.0
            if sla.get("state") == "overdue":
                score += 50
                overdue_h = abs(sla["resolution_due_in_hours"])
                why.append(f"SLA overdue by {_human_hours(overdue_h)}")
            elif sla.get("state") == "due_soon":
                score += 25
                why.append(f"SLA due in {_human_hours(sla['resolution_due_in_hours'])}")
            if sla.get("first_response_overdue"):
                score += 20
                why.append("no first response yet (overdue)")
            if t.get("priority") == "urgent":
                score += 25
                why.append("urgent")
            elif t.get("priority") == "high":
                score += 10
                why.append("high priority")
            if t.get("is_escalated"):
                score += 15
                why.append("escalated")
            if sig.get("payment_related"):
                score += 15
                why.append(f"payment issue ({sig.get('intent')})" +
                           (" with Razorpay refs" if sig.get("payment_refs") else ""))
            if not t.get("responder_id"):
                score += 10
                why.append("unassigned")
            if t.get("content_flags"):
                score += 5
                why.append("possible prompt injection: human review")
            scored.append((score, overdue_h, t, why))
        # highest score first; among equals, the longest-overdue ticket first
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        attention = [{"id": t["id"], "subject": t.get("subject"), "status": t.get("status"),
                      "priority": t.get("priority"), "intent": t.get("signals", {}).get("intent"),
                      "score": sc, "why": why, **({"source_url": t["source_url"]} if t.get("source_url") else {})}
                     for sc, _, t, why in scored[:top] if sc > 0]
        payment = [{"id": t["id"], "intent": t["signals"].get("intent"),
                    "payment_refs": t["signals"].get("payment_refs", [])}
                   for t in tickets if t.get("signals", {}).get("payment_related")]
        overdue = sum(1 for t in tickets if (t.get("signals", {}).get("sla") or {}).get("state") == "overdue")
        due_soon = sum(1 for t in tickets if (t.get("signals", {}).get("sla") or {}).get("state") == "due_soon")
        return {
            "active_backlog": total,
            "analysed": len(tickets),
            "complete": len(tickets) >= total,
            "active_statuses": active,
            "overdue": overdue,
            "due_soon": due_soon,
            "unassigned": sum(1 for t in tickets if not t.get("responder_id")),
            "payment_related": len(payment),
            "by_intent": dict(sorted(by_intent.items(), key=lambda kv: -kv[1])),
            "by_priority": by_priority,
            "by_status": by_status,
            "needs_attention": attention,
            "payment_tickets": payment[:25],
            "note": ("Signals here come from what search returns (Freshdesk search may omit ticket "
                     "bodies); get_ticket gives the full picture. Pass payment_refs to Razorpay tools "
                     "to answer refund/payment questions from the source of truth."),
        }

    # ------------------------------------------------------------------- meta
    async def connector_status(self) -> dict:
        me = await self.c.whoami()
        contact = me.get("contact", {}) if isinstance(me, dict) else {}
        return {
            "connected": True,
            "freshdesk_url": self.c.creds.base_url,
            "authenticated_as": {"agent_id": me.get("id"), "name": contact.get("name")},
            "access": "read-only",
            "ticket_statuses": sorted((await self.statuses()).values()),
            "pii_redaction": self.n.redact_pii,
            "private_notes": "include" if self.n.include_private_notes else "exclude",
            "rate_limit": await self.c.rl.stats(),
        }
