"""Fictional Razorpay data, deliberately consistent with mock_server/data.py.

The point of this fixture is the disagreements. For each WooCommerce order
that claims something about money, the gateway here either confirms it or
contradicts it, which is what makes the reconciliation demo real rather than
a tautology.

Amounts are in **paise**, as the real Razorpay API returns them. A connector
that treats them as rupees is wrong by a factor of 100, which is exactly the
kind of error that is invisible in a demo and expensive in production.
"""

from __future__ import annotations

import time

KEY_ID = "rzp_test_mock0000000001"
KEY_SECRET = "mocksecret0000000000002"

NOW = int(time.time())
DAY = 86400


def ago(days: float) -> int:
    return int(NOW - days * DAY)


# ------------------------------------------------------------------ payments
PAYMENTS: dict[str, dict] = {
    # WooCommerce order 1101: clean, confirmed paid.
    "pay_NqX8aK2bLmTfQw": {
        "id": "pay_NqX8aK2bLmTfQw", "entity": "payment", "amount": 129800, "currency": "INR",
        "status": "captured", "order_id": "order_NqX8Z1pQrStUvW", "method": "upi",
        "captured": True, "amount_refunded": 0, "refund_status": None,
        "email": "ananya.rao@example.com", "contact": "+919845011223",
        "created_at": ago(9), "acquirer_data": {"rrn": "429712345678", "upi_transaction_id": "429712345678"},
    },
    # WooCommerce order 1102: the shop says refunded. The gateway says captured
    # with nothing refunded. This is the disagreement the connector exists for.
    "pay_NrT4bM9cPqWxYz": {
        "id": "pay_NrT4bM9cPqWxYz", "entity": "payment", "amount": 245000, "currency": "INR",
        "status": "captured", "order_id": "order_NrT4a8LkJhGfDs", "method": "upi",
        "captured": True, "amount_refunded": 0, "refund_status": None,
        "email": "vikram.shetty@example.com", "contact": "+919986044556",
        "created_at": ago(12), "acquirer_data": {"rrn": "429798765432", "upi_transaction_id": "429798765432"},
    },
    # WooCommerce order 1103: both payments really were captured. The double
    # charge is real, not a plugin artefact.
    "pay_NsK1cD4eFgHiJk": {
        "id": "pay_NsK1cD4eFgHiJk", "entity": "payment", "amount": 89900, "currency": "INR",
        "status": "captured", "order_id": "order_NsK1bZxYwVuTsR", "method": "upi",
        "captured": True, "amount_refunded": 0, "refund_status": None,
        "email": "meera.k@example.com", "contact": "+919740077889",
        "created_at": ago(1.2), "acquirer_data": {"rrn": "429817736520", "upi_transaction_id": "429817736520"},
    },
    "pay_NsK1dE5fGhIjKl": {
        "id": "pay_NsK1dE5fGhIjKl", "entity": "payment", "amount": 89900, "currency": "INR",
        "status": "captured", "order_id": "order_NsK1bZxYwVuTsR", "method": "upi",
        "captured": True, "amount_refunded": 0, "refund_status": None,
        "email": "meera.k@example.com", "contact": "+919740077889",
        "created_at": ago(1.15), "acquirer_data": {"rrn": "429817736521", "upi_transaction_id": "429817736521"},
    },
    # WooCommerce order 1106: partial refund, and the gateway agrees.
    "pay_NuV2hI3jKlMnOp": {
        "id": "pay_NuV2hI3jKlMnOp", "entity": "payment", "amount": 320000, "currency": "INR",
        "status": "captured", "order_id": "order_NuV2gF1eDcBaZy", "method": "card",
        "captured": True, "amount_refunded": 75000, "refund_status": "partial",
        "email": "ananya.rao@example.com", "contact": "+919845011223",
        "created_at": ago(5), "acquirer_data": {"auth_code": "114422"},
    },
    # WooCommerce order 1108.
    "pay_NwX5kL6mNoPqRs": {
        "id": "pay_NwX5kL6mNoPqRs", "entity": "payment", "amount": 52000, "currency": "INR",
        "status": "captured", "order_id": None, "method": "upi",
        "captured": True, "amount_refunded": 0, "refund_status": None,
        "email": "test.buyer@example.com", "contact": "+919000000000",
        "created_at": ago(0.25), "acquirer_data": {"rrn": "429811112222"},
    },
    # WooCommerce order 1110: subscription renewal.
    "pay_NyZ7mN8oPqRsTu": {
        "id": "pay_NyZ7mN8oPqRsTu", "entity": "payment", "amount": 99900, "currency": "INR",
        "status": "captured", "order_id": None, "method": "upi",
        "captured": True, "amount_refunded": 0, "refund_status": None,
        "email": "vikram.shetty@example.com", "contact": "+919986044556",
        "created_at": ago(4), "acquirer_data": {"rrn": "429833334444"},
    },
    # Not referenced by any WooCommerce order. Exists so the failed-refund and
    # pending-refund states can be tested without disturbing the store fixture.
    "pay_NzA9pQ1rStUvWx": {
        "id": "pay_NzA9pQ1rStUvWx", "entity": "payment", "amount": 150000, "currency": "INR",
        "status": "captured", "order_id": None, "method": "netbanking",
        "captured": True, "amount_refunded": 150000, "refund_status": "full",
        "email": "someone@example.com", "contact": "+919000011111",
        "created_at": ago(7), "acquirer_data": {},
    },
}

# ------------------------------------------------------------------- refunds
REFUNDS: dict[str, dict] = {
    # Confirmed: matches WooCommerce order 1106's refund row.
    "rfnd_NuV2zY9xWvUtSr": {
        "id": "rfnd_NuV2zY9xWvUtSr", "entity": "refund", "amount": 75000, "currency": "INR",
        "payment_id": "pay_NuV2hI3jKlMnOp", "status": "processed", "speed_processed": "normal",
        "created_at": ago(1), "receipt": None, "notes": {"reason": "damaged tin"},
        "acquirer_data": {"arn": "74567890123456789012345"},
    },
    # Failed at the gateway. Nothing in WooCommerce would ever show this, which
    # is the point: the shop shows a refund row, the money bounced back.
    "rfnd_NzA9failedRfnd": {
        "id": "rfnd_NzA9failedRfnd", "entity": "refund", "amount": 150000, "currency": "INR",
        "payment_id": "pay_NzA9pQ1rStUvWx", "status": "failed", "speed_processed": "normal",
        "created_at": ago(6), "receipt": None, "notes": {},
        "acquirer_data": {},
    },
}

REFUNDS_BY_PAYMENT: dict[str, list[str]] = {
    "pay_NuV2hI3jKlMnOp": ["rfnd_NuV2zY9xWvUtSr"],
    "pay_NzA9pQ1rStUvWx": ["rfnd_NzA9failedRfnd"],
}
