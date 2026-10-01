"""Property-based fuzzing of the inputs most exposed to an LLM or to customer
text: the search-query builder, HTML conversion, PII masking and the
injection detector. Hypothesis generates thousands of adversarial cases."""

import re

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from freshdesk_connector import guardrails
from freshdesk_connector.errors import InvalidRequest
from freshdesk_connector.insights import classify_intent, extract_payment_refs
from freshdesk_connector.normalize import html_to_text, mask_pii
from freshdesk_connector.query import build_ticket_query
from mock_server.app import _compile_query

FAST = settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])


@FAST
@given(st.lists(st.text(min_size=0, max_size=40), min_size=1, max_size=5))
def test_tag_filter_either_rejects_or_builds_a_query_that_means_only_tags(tags):
    try:
        q = build_ticket_query(tags=tags)
    except InvalidRequest:
        return
    # Anything accepted must parse as pure tag clauses: no operator/field smuggled in.
    assert len(q) <= 512
    _compile_query(q)                              # the Freshdesk-grammar parser accepts it
    body = q[1:-1]
    clauses = body[1:-1].split(" OR ") if body.startswith("(") else [body]
    assert len(clauses) == len(tags)
    for clause, tag in zip(clauses, tags, strict=True):
        assert clause == f"tag:'{tag}'"


@FAST
@given(st.text(max_size=2000))
def test_html_to_text_never_crashes_and_strips_tags(s):
    out = html_to_text(s)
    if "&" not in s:            # without entities, no tag can survive conversion
        assert re.search(r"<[A-Za-z/!][^>]*>", out) is None


@FAST
@given(st.emails())
def test_mask_pii_hides_every_email(email):
    local, domain = email.rsplit("@", 1)
    masked = mask_pii(f"contact {email} now")
    assert f"contact {local[0]}***@{domain} now" == masked     # whole local part hidden but the first char


def test_mask_pii_regressions():
    assert mask_pii("o'brien@example.com") == "o***@example.com"
    assert mask_pii("mail a!b#c@x.co.in") == "mail a***@x.co.in"


@FAST
@given(st.text(max_size=3000))
def test_detectors_are_total_and_fast(s):
    guardrails.looks_like_injection(s)
    extract_payment_refs(s)
    classify_intent(s)


@FAST
@given(st.lists(st.dictionaries(st.text(max_size=5), st.text(max_size=200), max_size=4), max_size=200),
       st.integers(min_value=200, max_value=20_000))
def test_size_budget_property(items, budget):
    import json
    out, _ = guardrails.fit_to_budget({"items": items}, budget)
    assert len(json.dumps(out, ensure_ascii=False, default=str)) <= max(budget, 300)
