"""Build Freshdesk filter-search queries from structured arguments.

Freshdesk's /api/v2/search/* endpoints take a mini query language, e.g.
    "status:2 AND (priority:3 OR priority:4) AND tag:'refund'"
We never let the model write that string directly: it would be an
injection surface and a constant source of syntax errors. Instead tools
accept typed arguments and this module assembles + validates the query.
"""

from __future__ import annotations

import re
from datetime import date, datetime

from .errors import InvalidRequest
from .normalize import PRIORITY_IDS, STATUS_IDS

MAX_QUERY_LEN = 512  # Freshdesk hard limit
_SAFE_STR = re.compile(r"^[\w .@+\-/:]{1,100}$", re.UNICODE)


def _str_lit(field: str, v: str) -> str:
    if not _SAFE_STR.match(v):
        raise InvalidRequest(f"Unsupported characters in {field}: {v!r}")
    return f"{field}:'{v}'"


def _date_lit(v: str, field: str) -> str:
    try:
        d = date.fromisoformat(v[:10]) if len(v) >= 10 else None
    except ValueError:
        d = None
    if d is None:
        raise InvalidRequest(f"{field} must be an ISO date YYYY-MM-DD, got {v!r}")
    return d.isoformat()


def _or_group(terms: list[str]) -> str:
    return terms[0] if len(terms) == 1 else "(" + " OR ".join(terms) + ")"


def build_ticket_query(
    *,
    status: list[str] | None = None,
    priority: list[str] | None = None,
    tags: list[str] | None = None,
    ticket_type: str | None = None,
    agent_id: int | None = None,
    group_id: int | None = None,
    created_after: str | None = None,
    created_before: str | None = None,
    updated_after: str | None = None,
    updated_before: str | None = None,
    due_before: str | None = None,
) -> str:
    clauses: list[str] = []
    if status:
        bad = [s for s in status if s not in STATUS_IDS]
        if bad:
            raise InvalidRequest(f"Unknown status {bad}; allowed: {sorted(STATUS_IDS)}")
        clauses.append(_or_group([f"status:{STATUS_IDS[s]}" for s in status]))
    if priority:
        bad = [p for p in priority if p not in PRIORITY_IDS]
        if bad:
            raise InvalidRequest(f"Unknown priority {bad}; allowed: {sorted(PRIORITY_IDS)}")
        clauses.append(_or_group([f"priority:{PRIORITY_IDS[p]}" for p in priority]))
    if tags:
        clauses.append(_or_group([_str_lit("tag", t) for t in tags]))
    if ticket_type:
        clauses.append(_str_lit("type", ticket_type))
    if agent_id is not None:
        clauses.append(f"agent_id:{int(agent_id)}")
    if group_id is not None:
        clauses.append(f"group_id:{int(group_id)}")
    for field, op, val in (
        ("created_at", ">", created_after),
        ("created_at", "<", created_before),
        ("updated_at", ">", updated_after),
        ("updated_at", "<", updated_before),
        ("due_by", "<", due_before),
    ):
        if val:
            clauses.append(f"{field}:{op}'{_date_lit(val, field)}'")
    if not clauses:
        raise InvalidRequest("search_tickets needs at least one filter; use list_tickets to browse")
    q = '"' + " AND ".join(clauses) + '"'
    if len(q) > MAX_QUERY_LEN:
        raise InvalidRequest(f"Query too long ({len(q)} > {MAX_QUERY_LEN}); narrow the filters")
    return q


def iso_or_none(v: str | None) -> str | None:
    if not v:
        return None
    try:
        datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError as e:
        raise InvalidRequest(f"Expected ISO-8601 timestamp, got {v!r}") from e
    return v
