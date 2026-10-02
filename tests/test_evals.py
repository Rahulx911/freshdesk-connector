"""The eval dataset is part of the deliverable, so it is tested like code."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.run import CASES, PERSONAS, contains, summarise

CASE_DATA = json.loads(Path(CASES).read_text())["cases"]
TOOLS = {
    "list_orders", "search_orders", "get_order", "list_order_refunds",
    "list_products", "get_product", "find_customers", "get_customer",
    "customer_order_history", "store_pulse", "connector_status",
}


def test_nineteen_cases():
    assert len(CASE_DATA) == 19


def test_case_ids_are_unique():
    ids = [c["id"] for c in CASE_DATA]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("case", CASE_DATA, ids=[c["id"] for c in CASE_DATA])
def test_case_is_well_formed(case):
    assert case["persona"] in PERSONAS
    assert case["question"].strip()
    assert case["oracle"], "every case needs reference calls for oracle mode"
    for step in case["oracle"]:
        assert step["tool"] in TOOLS, step["tool"]
        assert isinstance(step["args"], dict)
    for tool in case.get("must_call", []):
        assert tool in TOOLS


def test_every_case_asserts_something():
    """A case with no checks would pass vacuously and hide a regression."""
    for case in CASE_DATA:
        assertions = (
            case.get("facts_all", []) + case.get("facts_any", [])
            + case.get("forbidden", []) + case.get("must_not_claim", [])
            + list((case.get("oracle_flags") or {}).keys())
            + list((case.get("oracle_error") or {}).keys())
        )
        assert assertions, f"{case['id']} asserts nothing"


def test_safety_cases_guard_against_false_refund_claims():
    """The expensive failure mode is claiming money moved when it did not."""
    unconfirmed = next(c for c in CASE_DATA if c["id"] == "refund_recorded_but_unconfirmed")
    assert unconfirmed["must_not_claim"]
    assert any("refund" in claim.lower() for claim in unconfirmed["must_not_claim"])


def test_digit_matching_is_whole_number():
    assert contains("order 1103 is fine", "1103")
    assert not contains("order 11030 is fine", "1103")
    assert not contains("total 2.1103", "1103")


def test_summarise_counts_by_category():
    out = summarise([
        {"category": "payments", "passed": True},
        {"category": "payments", "passed": False},
        {"category": "orders", "passed": True},
    ])
    assert out["cases"] == 3
    assert out["passed"] == 2
    assert out["by_category"]["payments"] == "1/2"
