"""Deterministic, entirely fictional helpdesk data for a made-up D2C brand
("Kettle & Leaf", an online tea store). No real people or customers."""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


AGENT_ME = {
    "id": 9001,
    "available": True,
    "occasional": False,
    "contact": {"name": "Demo Support Agent", "email": "agent@kettleandleaf.example"},
}

COMPANIES = [
    {"id": 501, "name": "Brewhouse Cafes", "domains": ["brewhouse.example"], "industry": "Food & Beverage",
     "account_tier": "Premium", "health_score": "At risk", "description": "Wholesale cafe chain"},
    {"id": 502, "name": "Brightlane Offices", "domains": ["brightlane.example"], "industry": "Real estate",
     "account_tier": "Basic", "health_score": "Happy", "description": "Office pantry supplier"},
    {"id": 503, "name": "Bloom Hotels", "domains": ["bloomhotels.example"], "industry": "Hospitality",
     "account_tier": "Premium", "health_score": "Doing okay", "description": "Boutique hotel group"},
]
for i, c in enumerate(COMPANIES):
    c["created_at"] = iso(NOW - timedelta(days=400 - i * 30))
    c["updated_at"] = iso(NOW - timedelta(days=5 + i))

_NAMES = [
    ("Asha Verma", "asha.verma@example.com", "+91 90000 00001", None),
    ("Kabir Nair", "kabir.nair@example.com", "+91 90000 00002", None),
    ("Meera Iyer", "meera@brewhouse.example", "+91 90000 00003", 501),
    ("Rohan Das", "rohan@brewhouse.example", None, 501),
    ("Ishita Rao", "ishita@brightlane.example", "+91 90000 00005", 502),
    ("Vikram Shah", "vikram.shah@example.com", None, None),
    ("Neha Kulkarni", "neha@bloomhotels.example", "+91 90000 00007", 503),
    ("Arjun Mehta", "arjun.mehta@example.com", "+91 90000 00008", None),
]
CONTACTS = []
for i, (name, email, phone, company) in enumerate(_NAMES):
    CONTACTS.append({
        "id": 1000 + i, "name": name, "email": email, "phone": phone, "mobile": None,
        "company_id": company, "active": True, "job_title": None, "language": "en",
        "time_zone": "Chennai", "tags": ["wholesale"] if company else ["d2c"],
        "created_at": iso(NOW - timedelta(days=300 - i * 10)),
        "updated_at": iso(NOW - timedelta(days=3 + i)),
    })

_SCENARIOS = [
    ("Order #KL-{n} not delivered yet", "Delivery", ["shipping"], "Question",
     "<p>Hi, my order <b>#KL-{n}</b> was due 3 days ago and tracking hasn't moved. Please help.</p>"),
    ("Refund not received for cancelled order", "Refund", ["refund", "payments"], "Refund",
     "<p>I cancelled my order on the same day but the refund hasn't reached my account. "
     "UPI ref ending 4421.</p>"),
    ("Autopay subscription charged twice", "Billing", ["subscription", "payments"], "Problem",
     "<div>My monthly tea subscription was debited twice this month. Please reverse one.</div>"),
    ("Damaged packaging - Assam CTC 1kg", "Quality", ["damaged"], "Problem",
     "<p>The pouch was torn on arrival. Photos attached.</p>"),
    ("Bulk pricing for 50 kg monthly", "Sales", ["wholesale"], "Question",
     "<p>We'd like a quote for 50kg/month of Darjeeling first flush for our outlets.</p>"),
    ("Change delivery address", "Delivery", ["shipping"], "Question",
     "<p>Can you ship order #KL-{n} to my office instead?</p>"),
    ("Coupon TEA20 not applying", "Checkout", ["promo"], "Problem",
     "<p>Checkout says coupon is invalid but your email said it's valid till month end.</p>"),
    ("GST invoice required", "Billing", ["invoice"], "Question",
     "<p>Please share a GST invoice with our GSTIN for the last 3 orders.</p>"),
]

rng = random.Random(42)
TICKETS: list[dict] = []
CONVERSATIONS: dict[int, list[dict]] = {}

for i in range(36):
    subj, _cat, tags, ttype, body = _SCENARIOS[i % len(_SCENARIOS)]
    contact = CONTACTS[i % len(CONTACTS)]
    n = 10200 + i
    # spread creation across ~75 days so the 30-day default window matters
    created = NOW - timedelta(days=int(75 - i * 2.1), hours=rng.randint(0, 20))
    updated = created + timedelta(hours=rng.randint(1, 72))
    if updated > NOW:
        updated = NOW - timedelta(minutes=5)
    status = [2, 2, 3, 4, 5, 2, 6, 3][i % 8]
    priority = [1, 2, 2, 3, 4, 2, 1, 3][(i * 3) % 8]
    tid = 1 + i
    t = {
        "id": tid,
        "subject": subj.format(n=n),
        "description": body.format(n=n),
        "description_text": None,
        "status": status,
        "priority": priority,
        "source": [1, 2, 3, 7][i % 4],
        "type": ttype,
        "tags": list(tags),
        "requester_id": contact["id"],
        "responder_id": 9001 if status != 2 or i % 2 else None,
        "group_id": 77 if "payments" in tags else 78,
        "company_id": contact["company_id"],
        "created_at": iso(created),
        "updated_at": iso(updated),
        "due_by": iso(created + timedelta(days=3)),
        "fr_due_by": iso(created + timedelta(hours=8)),
        "is_escalated": priority == 4,
        "custom_fields": {"cf_order_id": f"KL-{n}", "cf_channel": "website"},
    }
    TICKETS.append(t)

    convs = [{
        "id": tid * 100 + 1, "incoming": False, "private": False, "user_id": 9001,
        "body": "<p>Thanks for reaching out, we're looking into this.</p>",
        "body_text": "Thanks for reaching out, we're looking into this.",
        "created_at": iso(created + timedelta(hours=1)), "attachments": [],
    }]
    if i % 3 == 0:
        convs.append({
            "id": tid * 100 + 2, "incoming": False, "private": True, "user_id": 9001,
            "body": "<p>Internal: courier escalation raised, customer phone +91 90000 11111.</p>",
            "body_text": "Internal: courier escalation raised, customer phone +91 90000 11111.",
            "created_at": iso(created + timedelta(hours=2)), "attachments": [],
        })
    if i % 2 == 0:
        convs.append({
            "id": tid * 100 + 3, "incoming": True, "private": False, "user_id": contact["id"],
            "body": "<p>Any update on this?</p>", "body_text": "Any update on this?",
            "created_at": iso(created + timedelta(hours=20)),
            "attachments": ([{"name": "photo.jpg", "content_type": "image/jpeg", "size": 182311}]
                            if "damaged" in tags else []),
        })
    # one long thread to exercise conversation pagination
    if tid == 5:
        for k in range(45):
            convs.append({
                "id": tid * 100 + 10 + k, "incoming": k % 2 == 0, "private": False,
                "user_id": contact["id"] if k % 2 == 0 else 9001,
                "body": f"<p>Follow-up message {k + 1}</p>", "body_text": f"Follow-up message {k + 1}",
                "created_at": iso(created + timedelta(hours=30 + k)), "attachments": [],
            })
    CONVERSATIONS[tid] = convs
