"""Turn raw Freshdesk JSON into compact, agent-friendly records.

Why: raw tickets carry numeric enums (status=2), HTML bodies and dozens of
fields the model doesn't need. Mapping enums to words, stripping HTML and
truncating long text cuts tokens and removes a whole class of
hallucinations ("status 4 means pending?")."""

from __future__ import annotations

import html
import re
from typing import Any

# Freshdesk's built-in statuses. Accounts add custom ones (e.g. 6 "Waiting on Customer");
# those are loaded per account from /api/v2/ticket_fields (see FreshdeskService.statuses).
STATUS = {2: "open", 3: "pending", 4: "resolved", 5: "closed"}
STATUS_IDS = {v: k for k, v in STATUS.items()}
PRIORITY = {1: "low", 2: "medium", 3: "high", 4: "urgent"}
PRIORITY_IDS = {v: k for k, v in PRIORITY.items()}
SOURCE = {1: "email", 2: "portal", 3: "phone", 7: "chat", 9: "feedback_widget",
          10: "outbound_email"}

_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_RE = re.compile(r"</?(p|div|br|li|tr|h\d)[^>]*>", re.I)
_WS_RE = re.compile(r"[ \t]+")
_NL_RE = re.compile(r"\n{3,}")
_EMAIL_RE = re.compile(r"([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*(@[A-Za-z0-9.-]+\.[A-Za-z]{2,})")
_PHONE_RE = re.compile(r"(?<!\d)(\+?\d[\d\s-]{7,}\d)(?!\d)")


def slugify(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", label.strip().lower()).strip("_")


def html_to_text(s: str | None) -> str:
    if not s:
        return ""
    s = _BLOCK_RE.sub("\n", s)
    s = _TAG_RE.sub("", s)
    s = html.unescape(s)
    s = _WS_RE.sub(" ", s)
    return _NL_RE.sub("\n\n", s).strip()


def truncate(s: str, limit: int) -> str:
    if limit <= 0 or len(s) <= limit:
        return s
    return s[:limit].rstrip() + f"… [truncated, {len(s) - limit} more chars]"


def mask_pii(s: str) -> str:
    s = _EMAIL_RE.sub(r"\1***\2", s)
    return _PHONE_RE.sub(lambda m: "***" + re.sub(r"\D", "", m.group(1))[-3:], s)


class Normalizer:
    def __init__(self, *, redact_pii: bool = False, max_body_chars: int = 2000):
        self.redact_pii = redact_pii
        self.max_body_chars = max_body_chars
        self.status_names: dict[int, str] = dict(STATUS)

    def _pii(self, v: Any) -> Any:
        if self.redact_pii and isinstance(v, str):
            return mask_pii(v)
        return v

    def _body(self, raw_text: str | None, raw_html: str | None) -> str:
        text = raw_text if raw_text else html_to_text(raw_html)
        return self._pii(truncate(text, self.max_body_chars))

    def _status(self, sid: object) -> object:
        if isinstance(sid, int):
            return self.status_names.get(sid, f"status_{sid}")
        return sid

    def ticket(self, t: dict, *, include_body: bool = False) -> dict:
        out = {
            "id": t.get("id"),
            "subject": t.get("subject"),
            "status": self._status(t.get("status")),
            "priority": PRIORITY.get(t.get("priority"), t.get("priority")),
            "source": SOURCE.get(t.get("source"), t.get("source")),
            "type": t.get("type"),
            "tags": t.get("tags") or [],
            "requester_id": t.get("requester_id"),
            "responder_id": t.get("responder_id"),
            "group_id": t.get("group_id"),
            "company_id": t.get("company_id"),
            "created_at": t.get("created_at"),
            "updated_at": t.get("updated_at"),
            "due_by": t.get("due_by"),
            "fr_due_by": t.get("fr_due_by"),
            "is_escalated": t.get("is_escalated"),
        }
        if t.get("custom_fields"):
            out["custom_fields"] = {k: v for k, v in t["custom_fields"].items() if v is not None}
        if include_body or "description_text" in t or "description" in t:
            if t.get("description_text") or t.get("description"):
                out["description"] = self._body(t.get("description_text"), t.get("description"))
        if isinstance(t.get("requester"), dict):
            out["requester"] = self.contact(t["requester"])
        if isinstance(t.get("stats"), dict):
            s = t["stats"]
            out["stats"] = {k: s.get(k) for k in
                            ("first_responded_at", "agent_responded_at", "requester_responded_at",
                             "resolved_at", "closed_at") if s.get(k)}
        if isinstance(t.get("conversations"), list):
            out["conversations"] = [self.conversation(c) for c in t["conversations"]]
        return {k: v for k, v in out.items() if v not in (None, [], {})}

    def conversation(self, c: dict) -> dict:
        out = {
            "id": c.get("id"),
            "from": "agent" if not c.get("incoming") else "customer",
            "private_note": bool(c.get("private")),
            "user_id": c.get("user_id"),
            "created_at": c.get("created_at"),
            "body": self._body(c.get("body_text"), c.get("body")),
        }
        if c.get("attachments"):
            out["attachments"] = [
                {"name": a.get("name"), "content_type": a.get("content_type"), "size": a.get("size")}
                for a in c["attachments"]
            ]
        return out

    def contact(self, c: dict) -> dict:
        out = {
            "id": c.get("id"),
            "name": c.get("name"),
            "email": self._pii(c.get("email")),
            "phone": self._pii(c.get("phone")),
            "mobile": self._pii(c.get("mobile")),
            "company_id": c.get("company_id"),
            "job_title": c.get("job_title"),
            "language": c.get("language"),
            "time_zone": c.get("time_zone"),
            "active": c.get("active"),
            "tags": c.get("tags") or [],
            "created_at": c.get("created_at"),
            "updated_at": c.get("updated_at"),
        }
        return {k: v for k, v in out.items() if v not in (None, [], {})}

    def company(self, c: dict) -> dict:
        out = {
            "id": c.get("id"),
            "name": c.get("name"),
            "description": c.get("description"),
            "domains": c.get("domains") or [],
            "industry": c.get("industry"),
            "account_tier": c.get("account_tier"),
            "health_score": c.get("health_score"),
            "renewal_date": c.get("renewal_date"),
            "created_at": c.get("created_at"),
            "updated_at": c.get("updated_at"),
        }
        return {k: v for k, v in out.items() if v not in (None, [], {})}
