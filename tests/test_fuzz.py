"""Property-based tests. The invariants that must hold for any input."""

from __future__ import annotations

import json

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from woocommerce_connector import query as q
from woocommerce_connector.errors import InvalidRequest
from woocommerce_connector.guardrails import fit_response, scan_injection
from woocommerce_connector.insights import derive, scan_text
from woocommerce_connector.normalize import mask_email, mask_phone

SETTINGS = settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow], deadline=None)


@given(st.text())
@SETTINGS
def test_scan_text_never_raises(text):
    out = scan_text(text)
    assert isinstance(out, dict)
    for key, value in out.items():
        if key == "signature_verified":
            continue
        assert isinstance(value, list)


@given(st.text())
@SETTINGS
def test_injection_scan_never_raises(text):
    assert isinstance(scan_injection(text), list)


@given(st.text(min_size=1))
@SETTINGS
def test_masked_email_never_contains_a_long_local_part(raw):
    masked = mask_email(raw) or ""
    local = raw.split("@")[0]
    if "@" in raw and len(local) > 2:
        assert local not in masked


@given(st.text())
@SETTINGS
def test_masked_phone_keeps_at_most_four_digits(raw):
    masked = mask_phone(raw) or ""
    digits = [c for c in masked if c.isdigit()]
    assert len(digits) <= 4


@given(st.integers())
@SETTINGS
def test_per_page_either_validates_or_raises(n):
    try:
        out = q.clean_per_page(n)
        assert 1 <= out <= q.MAX_PER_PAGE
    except InvalidRequest:
        pass


@given(st.text(max_size=200))
@SETTINGS
def test_search_either_validates_or_raises(text):
    try:
        out = q.clean_search(text)
        assert out is None or len(out) <= q.MAX_SEARCH_LEN
    except InvalidRequest:
        pass


@given(st.dictionaries(st.text(max_size=12), st.text(max_size=40), max_size=8))
@SETTINGS
def test_derive_never_raises_on_arbitrary_orders(blob):
    sig = derive(dict(blob))
    assert sig.payment_state in {
        "unknown", "paid", "failed", "cancelled", "fully_refunded",
        "partially_refunded", "awaiting_payment",
    }
    json.dumps(sig.to_dict(), default=str)


@given(st.lists(st.dictionaries(st.text(max_size=6), st.text(max_size=60), max_size=4), max_size=60))
@SETTINGS
def test_fit_response_always_returns_valid_json(items):
    out = fit_response({"items": items}, budget=800)
    json.dumps(out, default=str)
    assert len(out["items"]) <= len(items)
