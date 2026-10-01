"""A small Freshdesk API v2 look-alike for offline demos and tests.

Faithful on the parts the connector depends on:
  * Basic auth with API key as username (401 otherwise)
  * page/per_page pagination with `Link: <...>; rel="next"`
  * tickets list default window = created in last 30 days unless updated_since
  * /search/tickets query language subset, 30 per page, max 10 pages, `total`
  * per-minute account rate limit with X-RateLimit-* headers and 429 + Retry-After
  * an optional "flaky" mode returning 503s to exercise retries

Run:  uvicorn mock_server.app:app --port 8765
"""

from __future__ import annotations

import base64
import os
import re
import time
from collections import deque
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import data as D

MOCK_API_KEY = "mock-api-key-123"


def _parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def create_app(*, rate_limit_per_min: int | None = None, api_key: str = MOCK_API_KEY,
               fail_first_n: int = 0, external_usage: int = 0, clock=time.monotonic) -> FastAPI:
    """external_usage: requests already spent this minute by the merchant's *other*
    integrations (Freshdesk quotas are per account, shared by every app)."""
    limit = rate_limit_per_min or int(os.environ.get("MOCK_RATE_LIMIT", "100"))
    state = {"hits": deque([clock()] * external_usage), "fails_left": fail_first_n, "requests": 0}
    app = FastAPI(title="Mock Freshdesk")
    app.state.mock = state

    @app.middleware("http")
    async def gate(request: Request, call_next):
        state["requests"] += 1
        auth = request.headers.get("authorization", "")
        ok = False
        if auth.startswith("Basic "):
            try:
                user = base64.b64decode(auth[6:]).decode().split(":", 1)[0]
                ok = user == api_key
            except Exception:
                ok = False
        if not ok:
            return JSONResponse({"code": "invalid_credentials", "message": "You have to be logged in to perform this action."}, status_code=401)

        now = clock()
        hits = state["hits"]
        while hits and now - hits[0] >= 60:
            hits.popleft()
        if len(hits) >= limit:
            retry = max(1, int(60 - (now - hits[0])) + 1)
            return JSONResponse({"message": "Rate limit exceeded"}, status_code=429,
                                headers={"Retry-After": str(retry), "X-RateLimit-Total": str(limit),
                                         "X-RateLimit-Remaining": "0"})
        hits.append(now)

        if state["fails_left"] > 0:
            state["fails_left"] -= 1
            return JSONResponse({"message": "Service Unavailable"}, status_code=503)

        resp = await call_next(request)
        resp.headers["X-RateLimit-Total"] = str(limit)
        resp.headers["X-RateLimit-Remaining"] = str(max(0, limit - len(hits)))
        resp.headers["X-RateLimit-Used-CurrentRequest"] = "1"
        return resp

    def paginate(request: Request, items: list):
        try:
            page = int(request.query_params.get("page", 1))
            per_page = int(request.query_params.get("per_page", 30))
        except ValueError:
            return JSONResponse({"description": "Validation failed", "errors": [
                {"field": "page", "message": "It should be a Positive Integer", "code": "invalid_value"}]},
                status_code=400)
        if per_page > 100 or per_page < 1 or page < 1:
            return JSONResponse({"description": "Validation failed", "errors": [
                {"field": "per_page", "message": "Must be between 1 and 100", "code": "invalid_value"}]},
                status_code=400)
        start = (page - 1) * per_page
        chunk = items[start:start + per_page]
        headers = {}
        if start + per_page < len(items):
            qp = dict(request.query_params)
            qp["page"] = str(page + 1)
            q = "&".join(f"{k}={v}" for k, v in qp.items())
            headers["Link"] = f'<{request.url.path}?{q}>; rel="next"'
        return JSONResponse(chunk, headers=headers)

    @app.get("/api/v2/agents/me")
    async def me():
        return D.AGENT_ME

    @app.get("/api/v2/tickets")
    async def list_tickets(request: Request):
        qp = request.query_params
        items = list(D.TICKETS)
        if "updated_since" in qp:
            since = _parse_dt(qp["updated_since"])
            items = [t for t in items if _parse_dt(t["updated_at"]) >= since]
        else:
            cutoff = datetime.now(timezone.utc) - timedelta(days=30)
            items = [t for t in items if _parse_dt(t["created_at"]) >= cutoff]
        if "email" in qp:
            ids = {c["id"] for c in D.CONTACTS if c["email"] == qp["email"].lower()}
            items = [t for t in items if t["requester_id"] in ids]
        if "requester_id" in qp:
            items = [t for t in items if t["requester_id"] == int(qp["requester_id"])]
        if "company_id" in qp:
            items = [t for t in items if t["company_id"] == int(qp["company_id"])]
        key = qp.get("order_by", "created_at")
        items.sort(key=lambda t: t[key], reverse=qp.get("order_type", "desc") == "desc")
        # Freshdesk list omits description unless include=description
        items = [{k: v for k, v in t.items() if k not in ("description", "description_text")} for t in items]
        return paginate(request, items)

    @app.get("/api/v2/tickets/{tid}")
    async def get_ticket(tid: int, request: Request):
        t = next((t for t in D.TICKETS if t["id"] == tid), None)
        if not t:
            return JSONResponse({"code": "access_denied"} if tid == 403 else {}, status_code=404)
        out = dict(t)
        include = request.query_params.get("include", "")
        if "requester" in include:
            out["requester"] = next(c for c in D.CONTACTS if c["id"] == t["requester_id"])
        if "stats" in include:
            out["stats"] = {"first_responded_at": D.CONVERSATIONS[tid][0]["created_at"],
                            "resolved_at": t["updated_at"] if t["status"] in (4, 5) else None}
        return out

    @app.get("/api/v2/tickets/{tid}/conversations")
    async def convs(tid: int, request: Request):
        if tid not in D.CONVERSATIONS:
            return JSONResponse({}, status_code=404)
        return paginate(request, D.CONVERSATIONS[tid])

    @app.get("/api/v2/search/tickets")
    async def search(request: Request):
        raw = request.query_params.get("query", "")
        try:
            pred = _compile_query(raw)
            page = int(request.query_params.get("page", 1))
            if not 1 <= page <= 10:
                raise ValueError("page must be 1..10")
        except ValueError as e:
            return JSONResponse({"description": "Validation failed",
                                 "errors": [{"field": "query", "message": str(e), "code": "invalid_value"}]},
                                status_code=400)
        hits = [t for t in D.TICKETS if pred(t)]
        hits.sort(key=lambda t: t["updated_at"], reverse=True)
        chunk = hits[(page - 1) * 30: page * 30]
        return {"results": chunk, "total": len(hits)}

    @app.get("/api/v2/contacts")
    async def contacts(request: Request):
        qp = request.query_params
        items = list(D.CONTACTS)
        if "email" in qp:
            items = [c for c in items if c["email"] == qp["email"].lower()]
        for f in ("phone", "mobile"):
            if f in qp:
                want = re.sub(r"\D", "", qp[f])
                items = [c for c in items if c.get(f) and re.sub(r"\D", "", c[f]).endswith(want[-10:])]
        return paginate(request, items)

    @app.get("/api/v2/contacts/autocomplete")
    async def contacts_ac(term: str):
        return [{"id": c["id"], "name": c["name"]} for c in D.CONTACTS
                if any(p.lower().startswith(term.lower()) for p in [c["name"], *c["name"].split()])]

    @app.get("/api/v2/contacts/{cid}")
    async def contact(cid: int):
        c = next((c for c in D.CONTACTS if c["id"] == cid), None)
        return c if c else JSONResponse({}, status_code=404)

    @app.get("/api/v2/companies/autocomplete")
    async def companies_ac(name: str):
        return {"companies": [{"id": c["id"], "name": c["name"]} for c in D.COMPANIES
                              if c["name"].lower().startswith(name.lower())]}

    @app.get("/api/v2/companies/{cid}")
    async def company(cid: int):
        c = next((c for c in D.COMPANIES if c["id"] == cid), None)
        return c if c else JSONResponse({}, status_code=404)

    return app


# ---------------------------------------------------------- query language
_TERM_RE = re.compile(r"^(\w+):(>|<)?(?:'([^']*)'|(\d+))$")
_FIELD_MAP = {"agent_id": "responder_id", "tag": "tags"}
_DATE_FIELDS = {"created_at", "updated_at", "due_by", "fr_due_by"}


def _term(expr: str):
    m = _TERM_RE.match(expr.strip())
    if not m:
        raise ValueError(f"Invalid term: {expr!r}")
    field, op, sval, nval = m.groups()
    key = _FIELD_MAP.get(field, field)
    if field in _DATE_FIELDS:
        if not op or sval is None:
            raise ValueError(f"{field} needs :> or :< with a quoted date")
        bound = sval[:10]
        return (lambda t: t[key][:10] > bound) if op == ">" else (lambda t: t[key][:10] < bound)
    val = int(nval) if nval is not None else sval
    if key == "tags":
        return lambda t: val in t["tags"]
    return lambda t: t.get(key) == val


def _compile_query(raw: str):
    q = raw.strip()
    if not (q.startswith('"') and q.endswith('"')) or len(q) > 512:
        raise ValueError("query must be wrapped in double quotes and <= 512 chars")
    q = q[1:-1]
    preds = []
    for clause in re.split(r"\s+AND\s+", q):
        clause = clause.strip()
        if clause.startswith("(") and clause.endswith(")"):
            ors = [_term(x) for x in re.split(r"\s+OR\s+", clause[1:-1])]
            preds.append(lambda t, ors=ors: any(p(t) for p in ors))
        else:
            preds.append(_term(clause))
    return lambda t: all(p(t) for p in preds)


app = create_app()
