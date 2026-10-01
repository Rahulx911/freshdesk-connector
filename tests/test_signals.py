"""Payment-aware signals, support_pulse, and the audit fixes (status cache,
HTML noise, size budget, registry hot-reload, key rotation, readiness)."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from freshdesk_connector import guardrails
from freshdesk_connector.errors import AuthError, ConfigError, InvalidRequest, RateLimited
from freshdesk_connector.insights import classify_intent, extract_payment_refs, sla_state
from freshdesk_connector.normalize import html_to_text
from freshdesk_connector.tenancy import RegistryWatcher, hash_token


def _types(refs):
    return {(r["type"], r["value"]) for r in refs}


# ------------------------------------------------------------ payment refs
@pytest.mark.parametrize("text,expected", [
    ("Payment ID pay_29QQoUBi66xm2f failed", {("razorpay_payment_id", "pay_29QQoUBi66xm2f")}),
    ("order_29QQoUBi66xm2f / rfnd_AbCdEfGhIjKlMn", {("razorpay_order_id", "order_29QQoUBi66xm2f"),
                                                     ("razorpay_refund_id", "rfnd_AbCdEfGhIjKlMn")}),
    ("mandate sub_00000000000001 charged", {("razorpay_subscription_id", "sub_00000000000001")}),
    ("UPI ref no: 412345678901", {("utr_or_rrn", "412345678901")}),
    ("UTR is HDFCR52024100112345", {("utr_or_rrn", "HDFCR52024100112345")}),
    ("ARN 74332745289230048593021 from bank", {("card_refund_arn", "74332745289230048593021")}),
    ("paid ₹1,499.50 and Rs. 899", {("amount_inr", "1499.50"), ("amount_inr", "899")}),
])
def test_extracts_payment_refs(text, expected):
    assert expected <= _types(extract_payment_refs(text))


@pytest.mark.parametrize("text", [
    "call me on 9876543210 or +91 98765 43210",          # phone numbers
    "pincode 411001, order number 123456789012",          # 12 digits without a payment keyword
    "pay_short and pay_waytoolongidentifier123",          # wrong id length
    "my GSTIN is 27ABCDE1234F1Z5",
    "unpaid order_ref",
])
def test_no_false_payment_refs(text):
    assert extract_payment_refs(text) == []


def test_merchant_order_id_from_custom_fields():
    refs = extract_payment_refs("hi", custom_fields={"cf_order_id": "KL-1", "cf_channel": "web"})
    assert refs == [{"type": "merchant_order_id", "value": "KL-1"}]


# ---------------------------------------------------------------- intents
@pytest.mark.parametrize("text,intent,payment", [
    ("I was charged twice for one order", "double_charge", True),
    ("Autopay mandate debited without consent", "autopay_mandate", True),
    ("Refund not received for cancelled order", "refund_status", True),
    ("Payment failed but amount deducted from my account", "payment_failed", True),
    ("When will my settlement be credited?", "settlement", True),
    ("Where is my order? Tracking hasn't moved", "delivery", False),
    ("Please share GST invoice", "invoice_tax", False),
    ("The pouch was torn on arrival", "product_quality", False),
    ("Hello there", "other", False),
])
def test_intent_classification(text, intent, payment):
    r = classify_intent(text)
    assert r["intent"] == intent and r["payment_related"] is payment
    if intent != "other":
        assert r["intent_evidence"]


# -------------------------------------------------------------------- SLA
def test_sla_states():
    now = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    t = lambda h: {"due_by": (now + timedelta(hours=h)).isoformat()}  # noqa: E731
    assert sla_state(t(-5), active=True, now=now)["state"] == "overdue"
    assert sla_state(t(2), active=True, now=now)["state"] == "due_soon"
    assert sla_state(t(30), active=True, now=now)["state"] == "on_track"
    assert sla_state(t(-5), active=False, now=now) is None          # resolved tickets carry no risk
    fr = {"fr_due_by": (now - timedelta(hours=1)).isoformat(), "stats": {"first_responded_at": None}}
    assert sla_state(fr, active=True, now=now)["first_response_overdue"] is True


# ------------------------------------------------------- tickets + service
async def test_ticket_signals_and_citation(make_service):
    svc = make_service()
    svc.n.portal_url = "https://kettleandleaf.freshdesk.com"
    t = await svc.get_ticket(2)
    sig = t["signals"]
    assert sig["intent"] == "refund_status" and sig["payment_related"]
    found = _types(sig["payment_refs"])
    assert ("razorpay_payment_id", "pay_KL10201QzXwVuT") in found
    assert ("razorpay_refund_id", "rfnd_KL10201RrSsTtU") in found     # from the agent's reply in the thread
    assert ("merchant_order_id", "KL-10201") in found
    assert t["source_url"] == "https://kettleandleaf.freshdesk.com/a/tickets/2"


async def test_support_pulse_ranks_with_reasons(make_service):
    svc = make_service()
    p = await svc.support_pulse(top=5)
    assert p["complete"] and p["analysed"] == p["active_backlog"] > 0
    assert set(p["active_statuses"]) >= {"open", "pending", "waiting_on_customer"}
    assert "resolved" not in p["active_statuses"]
    scores = [a["score"] for a in p["needs_attention"]]
    assert scores == sorted(scores, reverse=True) and all(a["why"] for a in p["needs_attention"])
    top = p["needs_attention"][0]
    assert any("SLA overdue" in w for w in top["why"])
    assert p["payment_related"] == len(p["payment_tickets"]) > 0
    assert any(r["type"].startswith("razorpay_") for pt in p["payment_tickets"] for r in pt["payment_refs"])
    # cost: one ticket_fields read + ceil(analysed/30) searches
    assert svc.mock_state["paths"]["/api/v2/search/tickets"] == -(-p["analysed"] // 30)


async def test_support_pulse_validation(make_service):
    with pytest.raises(InvalidRequest):
        await make_service().support_pulse(max_tickets=10)


# ------------------------------------------------------------ audit fixes
async def test_transient_error_does_not_pin_default_statuses(make_service, monkeypatch):
    svc = make_service()
    real = svc.c.get_json
    calls = {"n": 0}

    async def flaky(path, params=None):
        if path == "/api/v2/ticket_fields" and calls["n"] == 0:
            calls["n"] += 1
            raise RateLimited("busy", retry_after=5)
        return await real(path, params)

    monkeypatch.setattr(svc.c, "get_json", flaky)
    first = await svc.statuses()
    assert 6 not in first                                   # fell back for this call only
    assert (await svc.statuses())[6] == "waiting_on_customer"   # next call retried and learned it


async def test_status_catalogue_ttl_and_single_flight(make_service):
    svc = make_service()
    now = [0.0]
    svc._clock = lambda: now[0]
    await asyncio.gather(*[svc.statuses() for _ in range(10)])
    assert svc.mock_state["paths"]["/api/v2/ticket_fields"] == 1          # one fetch for 10 callers
    await svc.statuses()
    assert svc.mock_state["paths"]["/api/v2/ticket_fields"] == 1          # cached
    now[0] += svc.STATUS_TTL_S + 1
    await svc.statuses()
    assert svc.mock_state["paths"]["/api/v2/ticket_fields"] == 2          # refreshed after TTL


async def test_auth_error_on_statuses_propagates(make_service):
    with pytest.raises(AuthError):
        await make_service(api_key="bad").statuses()


def test_html_noise_removed():
    html = ("<html><head><title>x</title><style>.a{color:red}</style></head><body>"
            "<!-- tracking --><script>alert(1)</script><p>Refund please</p></body></html>")
    assert html_to_text(html) == "Refund please"


def test_size_budget_is_strict_and_fast():
    big = {"items": [{"id": i, "body": "x" * 500} for i in range(2000)], "page": 1}
    out, cut = guardrails.fit_to_budget(big, 20_000)
    assert cut and len(json.dumps(out)) <= 20_000
    assert out["items_omitted_for_size"] == 2000 - len(out["items"]) and out["truncated_for_size"]


def _registry(path, tokens, key_env="FD_KEY_X"):
    path.write_text(json.dumps({
        "tenants": {"x": {"domain": "acme", "api_key_env": key_env}},
        "tokens": [{"name": f"t{i}", "tenant": "x", "sha256": hash_token(t)} for i, t in enumerate(tokens)],
    }))


def test_registry_hot_reload_revokes_without_restart(tmp_path):
    p = tmp_path / "tenants.json"
    _registry(p, ["old-token"])
    now = [0.0]
    w = RegistryWatcher(p, interval_s=5, clock=lambda: now[0])
    assert w.current().tenant_for_token("old-token") == "x"
    _registry(p, ["new-token"])                         # revoke old, issue new
    os.utime(p, ns=(p.stat().st_atime_ns, p.stat().st_mtime_ns + 1_000_000))
    assert w.current().tenant_for_token("old-token") == "x"     # not re-checked yet (rate-limited stat)
    now[0] += 6
    assert w.current().tenant_for_token("old-token") is None
    assert w.current().tenant_for_token("new-token") == "x" and w.reloads == 1


def test_broken_registry_edit_keeps_last_good(tmp_path):
    p = tmp_path / "tenants.json"
    _registry(p, ["tok"])
    now = [0.0]
    w = RegistryWatcher(p, interval_s=1, clock=lambda: now[0])
    p.write_text("{ not json")
    os.utime(p, ns=(p.stat().st_atime_ns, p.stat().st_mtime_ns + 1_000_000))
    now[0] += 2
    assert w.current().tenant_for_token("tok") == "x" and w.last_error


def test_key_rotation_rebuilds_client_and_readiness_is_per_tenant(tmp_path, monkeypatch):
    from freshdesk_connector.mcp_server import ServiceProvider, Settings
    p = tmp_path / "tenants.json"
    p.write_text(json.dumps({"tenants": {
        "a": {"domain": "acme", "api_key_env": "FD_KEY_A"},
        "b": {"domain": "beta", "api_key_env": "FD_KEY_B_MISSING"}}, "tokens": []}))
    monkeypatch.setenv("FD_KEY_A", "key-1")
    monkeypatch.delenv("FD_KEY_B_MISSING", raising=False)
    prov = ServiceProvider(Settings(), RegistryWatcher(p))
    s1 = prov.service("a")
    assert prov.service("a") is s1                       # cached while nothing changes
    monkeypatch.setenv("FD_KEY_A", "key-2")              # merchant rotated their Freshdesk key
    s2 = prov.service("a")
    assert s2 is not s1 and s2.c.creds.api_key == "key-2"
    with pytest.raises(ConfigError):
        prov.service("b")
    ready = asyncio.run(prov.ready())
    assert ready["ready"] is True and ready["degraded"] is True        # one bad tenant != replica down
    assert ready["tenants_missing_credentials"] == ["b"]
