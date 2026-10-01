"""Payment-aware signals extracted from tickets.

This is what makes the connector specific to Razorpay merchants rather than a
generic Freshdesk reader. A large share of D2C support tickets are really
*payment* questions ("refund not received", "charged twice", "autopay
debited"). The answer to those lives in Razorpay, not in Freshdesk. So for
every ticket we pull out:

* payment_refs: Razorpay entity ids (pay_, order_, rfnd_, sub_, ...), UPI/IMPS
  UTR or RRN numbers, card ARNs, the merchant's own order id and INR amounts.
  An Agent Studio agent can pass these straight to Razorpay's Payments /
  Refunds APIs and answer from the source of truth instead of guessing.
* intent: a deterministic, explainable classification (with the phrase that
  triggered it) so the agent or a router can pick the right playbook.
* sla: whether an active ticket is overdue or due soon, from Freshdesk's own
  due_by / fr_due_by.

All deterministic regex and rules: cheap, auditable, testable for false
positives, and they never send customer text to a third-party model.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

# Razorpay entity ids: <prefix>_<14 base62 chars>, e.g. pay_29QQoUBi66xm2f
RAZORPAY_PREFIXES = {
    "pay": "razorpay_payment_id", "order": "razorpay_order_id", "rfnd": "razorpay_refund_id",
    "sub": "razorpay_subscription_id", "inv": "razorpay_invoice_id", "plink": "razorpay_payment_link_id",
    "cust": "razorpay_customer_id", "setl": "razorpay_settlement_id", "disp": "razorpay_dispute_id",
    "qr": "razorpay_qr_code_id",
}
_RZP_RE = re.compile(r"\b(" + "|".join(RAZORPAY_PREFIXES) + r")_([A-Za-z0-9]{14})\b")
# A 12-digit UPI RRN / IMPS UTR is only trusted next to a payment keyword, so phone numbers,
# pincodes and order numbers don't get picked up.
_UTR_RE = re.compile(
    r"\b(?:utr|rrn|upi\s*(?:ref(?:erence)?|txn|transaction)(?:\s*(?:no|number|id))?|"
    r"bank\s*ref(?:erence)?|transaction\s*(?:id|ref(?:erence)?|no|number))\b[\s.:#-]*(?:is\s*)?"
    r"([0-9]{12}|[A-Z]{4}[A-Z0-9]{12,18})\b", re.I)
_ARN_RE = re.compile(r"\b(\d{23})\b")                       # card refund Acquirer Reference Number
_AMOUNT_RE = re.compile(r"(?:₹|\bRs\.?|\bINR)\s?([0-9]{1,3}(?:,[0-9]{2,3})*(?:\.[0-9]{1,2})?|[0-9]+(?:\.[0-9]{1,2})?)",
                        re.I)

PAYMENT_INTENTS = {"double_charge", "refund_status", "payment_failed", "autopay_mandate", "settlement"}

# (intent, pattern) in priority order: the first match is the primary intent.
_INTENT_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("double_charge", re.compile(r"\b(charged|debited|deducted|paid)\s+(twice|two\s+times|double)\b|"
                                 r"\b(double|duplicate)\s+(charge|debit|deduction|payment)", re.I)),
    ("autopay_mandate", re.compile(r"\b(auto-?pay|e-?mandate|mandate|standing\s+instruction|"
                                   r"recurring\s+(payment|debit)|subscription\s+(charge|debit|renew))", re.I)),
    ("refund_status", re.compile(r"\b(refund|reversal|reversed|money\s+back|chargeback)", re.I)),
    ("payment_failed", re.compile(r"\b(payment|transaction|txn)\s+(failed|declined|pending|stuck|unsuccessful)|"
                                  r"\b(amount|money)\s+(deducted|debited)\b.{0,40}\b(not|no)\b|"
                                  r"\b(deducted|debited)\s+but\b", re.I | re.S)),
    ("settlement", re.compile(r"\b(settlement|payout)s?\b", re.I)),
    ("delivery", re.compile(r"\b(deliver(y|ed)?|shipp(ing|ed)|tracking|courier|dispatch(ed)?|"
                            r"where\s+is\s+my\s+order|not\s+(yet\s+)?received)\b", re.I)),
    ("order_change", re.compile(r"\b(change|update)\s+(the\s+)?(delivery\s+)?address|cancel(lation)?\b", re.I)),
    ("product_quality", re.compile(r"\b(damaged|torn|broken|defective|expired|wrong\s+item)\b", re.I)),
    ("invoice_tax", re.compile(r"\b(invoice|gst(in)?|receipt|tax)\b", re.I)),
    ("pricing_sales", re.compile(r"\b(quote|bulk|wholesale|pricing|price\s+list)\b", re.I)),
    ("promo", re.compile(r"\b(coupon|promo|discount\s+code|voucher)\b", re.I)),
]


def _dedupe(refs: list[dict[str, str]]) -> list[dict[str, str]]:
    seen, out = set(), []
    for r in refs:
        k = (r["type"], r["value"])
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


def extract_payment_refs(*texts: str | None, custom_fields: dict[str, Any] | None = None) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    for text in texts:
        if not text:
            continue
        for m in _RZP_RE.finditer(text):
            refs.append({"type": RAZORPAY_PREFIXES[m.group(1)], "value": m.group(0)})
        for m in _UTR_RE.finditer(text):
            refs.append({"type": "utr_or_rrn", "value": m.group(1).upper()})
        for m in _ARN_RE.finditer(text):
            refs.append({"type": "card_refund_arn", "value": m.group(1)})
        for m in _AMOUNT_RE.finditer(text):
            refs.append({"type": "amount_inr", "value": m.group(1).replace(",", "")})
    for k, v in (custom_fields or {}).items():
        if v and isinstance(v, (str, int)) and "order" in k.lower():
            refs.append({"type": "merchant_order_id", "value": str(v)})
    return _dedupe(refs)


def classify_intent(*texts: str | None) -> dict[str, Any]:
    blob = "\n".join(t for t in texts if t)
    hits: list[tuple[str, str]] = []
    for intent, rx in _INTENT_RULES:
        m = rx.search(blob)
        if m:
            hits.append((intent, m.group(0).strip()))
    if not hits:
        return {"intent": "other", "payment_related": False}
    primary, evidence = hits[0]
    out: dict[str, Any] = {
        "intent": primary,
        "intent_evidence": evidence[:60],
        "payment_related": any(i in PAYMENT_INTENTS for i, _ in hits),
    }
    if len(hits) > 1:
        out["secondary_intents"] = [i for i, _ in hits[1:]]
    return out


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def sla_state(ticket: dict[str, Any], *, active: bool, now: datetime | None = None,
              due_soon_hours: float = 4.0) -> dict[str, Any] | None:
    """SLA view of an *active* ticket (closed/resolved tickets have no SLA risk)."""
    if not active:
        return None
    now = now or datetime.now(timezone.utc)
    out: dict[str, Any] = {}
    due = _parse(ticket.get("due_by"))
    if due is not None:
        hours = round((due - now).total_seconds() / 3600, 1)
        out["resolution_due_in_hours"] = hours
        out["state"] = "overdue" if hours < 0 else "due_soon" if hours <= due_soon_hours else "on_track"
    fr_due = _parse(ticket.get("fr_due_by"))
    stats = ticket.get("stats") if isinstance(ticket.get("stats"), dict) else None
    if fr_due is not None and fr_due < now and stats is not None and not stats.get("first_responded_at"):
        out["first_response_overdue"] = True
    return out or None
