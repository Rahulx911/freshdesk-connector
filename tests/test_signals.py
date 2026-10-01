"""Signal extraction, including the false-positive corpus.

Every pattern added to insights.py must come with a negative case here. The
cost of a wrong payment reference is a customer being told the wrong thing
about their money, so the bar for matching is deliberately high.
"""

from __future__ import annotations

import pytest

from mock_server import data as mock_data
from woocommerce_connector.insights import attention_score, derive, scan_text


def order(order_id: int) -> dict:
    return next(o for o in mock_data.ORDERS if o["id"] == order_id)


def test_clean_paid_order():
    sig = derive(order(1101))
    assert sig.gateway == "razorpay"
    assert sig.payment_state == "paid"
    assert sig.payment_refs["payment_id"] == ["pay_NqX8aK2bLmTfQw"]
    assert sig.payment_refs["gateway_order_id"] == ["order_NqX8Z1pQrStUvW"]
    assert sig.payment_refs["signature_verified"] is True
    assert sig.intent == "paid"
    assert not sig.reconciliation


def test_refund_without_gateway_id_is_flagged():
    """The signal the whole connector exists for."""
    sig = derive(order(1102))
    assert sig.payment_state == "fully_refunded"
    assert sig.reconciliation["status"] == "refund_not_confirmed_at_gateway"
    assert "refund_id" not in sig.payment_refs
    assert sig.intent == "refund_unconfirmed"
    assert sig.intent_evidence


def test_refund_with_gateway_id_is_confirmed():
    sig = derive(order(1106))
    assert sig.payment_state == "partially_refunded"
    assert sig.reconciliation["status"] == "confirmed"
    assert sig.payment_refs["refund_id"] == ["rfnd_NuV2zY9xWvUtSr"]
    assert sig.amounts["refunded_total"] == 750.0
    assert sig.amounts["net_total"] == 2450.0


def test_double_charge_detected():
    sig = derive(order(1103))
    assert len(sig.payment_refs["payment_id"]) == 2
    assert sig.reconciliation["status"] == "multiple_payments"
    assert sig.intent == "double_charge"
    # the labelled UTR in the customer note is picked up
    assert sig.payment_refs["bank_reference"] == ["429817736521"]


def test_failed_payment():
    sig = derive(order(1104))
    assert sig.payment_state == "failed"
    assert sig.intent == "payment_failed"


def test_aged_unpaid_order():
    sig = derive(order(1105))
    assert sig.payment_state == "awaiting_payment"
    assert sig.intent == "awaiting_payment"
    assert sig.age["days_since_created"] >= 5


def test_cash_on_delivery_is_not_razorpay():
    sig = derive(order(1107))
    assert sig.gateway == "cash_on_delivery"
    assert sig.intent == "cod_pending"
    assert not sig.payment_refs


def test_subscription_id_extracted():
    sig = derive(order(1110))
    assert sig.payment_refs["subscription_id"] == ["sub_NyZ7lM6nOpQrSt"]


def test_non_razorpay_gateway_has_no_refs():
    sig = derive(order(1109))
    assert sig.gateway == "bacs"
    assert not sig.payment_refs


# ------------------------------------------------------ false positive corpus
@pytest.mark.parametrize("text", [
    "Please pay_later if possible",              # prefix inside a word
    "my order_number is 1234",                   # underscore word, too short
    "call me on 9845011223",                     # bare phone number
    "the total was 429817736521 paise",          # bare digits, no label
    "subscription for a year",                   # the word, not an id
    "reference: pending",                        # label followed by a word
    "",
    None,
])
def test_no_false_positive_references(text):
    assert scan_text(text) == {}


@pytest.mark.parametrize("text,kind,value", [
    ("payment pay_ABCDEFGHIJ1234 captured", "payment_id", "pay_ABCDEFGHIJ1234"),
    ("refund rfnd_ZYXWVUTSRQ9876 issued", "refund_id", "rfnd_ZYXWVUTSRQ9876"),
    ("UTR: 429817736521", "bank_reference", "429817736521"),
    ("utr 123456789012", "bank_reference", "123456789012"),
    ("ARN: 74567890123456789012345", "card_arn", "74567890123456789012345"),
    ("rrn#  987654321098", "bank_reference", "987654321098"),
])
def test_true_positives(text, kind, value):
    assert scan_text(text)[kind] == [value]


def test_attention_ranks_reconciliation_gap_highest():
    scored = []
    for o in mock_data.ORDERS:
        sig = derive(o)
        score, why = attention_score(sig, o)
        if score:
            scored.append((score, o["id"], why))
    scored.sort(reverse=True)
    top_ids = [sid for _, sid, _ in scored[:2]]
    assert 1103 in top_ids   # double charge
    assert 1102 in top_ids   # unconfirmed refund
    assert all(w for _, _, w in scored), "every ranked order must carry reasons"
