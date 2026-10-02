"""The gateway connector and the cross-system verdict."""

from __future__ import annotations

import httpx
import pytest

from mock_razorpay import data as rzp_data
from mock_razorpay.app import app as rzp_app
from razorpay_connector.auth import Credentials
from razorpay_connector.client import RazorpayClient, _map_error
from razorpay_connector.reconcile import (
    detect_duplicate_capture,
    paise_to_major,
    reconcile,
)
from razorpay_connector.service import RazorpayService
from woocommerce_connector.errors import AuthError, InvalidRequest, NotFound
from woocommerce_connector.ratelimit import RateLimiter

CREDS = Credentials(rzp_data.KEY_ID, rzp_data.KEY_SECRET, "https://api.razorpay.test")


@pytest.fixture
async def service():
    client = RazorpayClient(CREDS, transport=httpx.ASGITransport(app=rzp_app),
                            rate_limiter=RateLimiter(10_000))
    try:
        yield RazorpayService(client)
    finally:
        await client.aclose()


# ------------------------------------------------------------------- units
def test_amounts_are_converted_out_of_paise():
    """The real API returns paise. Treating them as rupees is wrong by 100x."""
    assert paise_to_major(245000) == 2450.0
    assert paise_to_major(99900) == 999.0
    assert paise_to_major(1) == 0.01
    assert paise_to_major(None) is None
    assert paise_to_major("nonsense") is None


def test_key_mode_is_read_from_the_key_id():
    assert Credentials("rzp_test_abc", "s").mode == "test"
    assert Credentials("rzp_live_abc", "s").mode == "live"
    assert Credentials("something_else", "s").mode == "unknown"


def test_secret_never_appears_in_redacted_output():
    c = Credentials("rzp_test_" + "a" * 20, "supersecret" * 3)
    blob = repr(c.redacted())
    assert "supersecret" not in blob


# --------------------------------------------------------------- verdicts
def test_never_issued_when_shop_claims_a_refund_the_gateway_has_not():
    r = reconcile(shop_refunded=2450.0, payment={"id": "pay_x"}, refunds=[])
    assert r.verdict == "never_issued"
    assert "not actually been sent" in r.customer_safe_message
    assert r.action


def test_confirmed_when_the_gateway_processed_the_same_amount():
    r = reconcile(shop_refunded=750.0, payment={"id": "pay_x"},
                  refunds=[{"id": "rfnd_a", "status": "processed", "amount": 750.0,
                            "card_arn": "74567890123456789012345"}])
    assert r.verdict == "confirmed"
    assert "74567890123456789012345" in r.customer_safe_message


def test_in_flight_when_the_refund_is_pending():
    r = reconcile(shop_refunded=500.0, payment={"id": "pay_x"},
                  refunds=[{"id": "rfnd_a", "status": "pending", "amount": 500.0}])
    assert r.verdict == "in_flight"
    assert "still being processed" in r.customer_safe_message


def test_failed_refund_is_urgent_and_names_an_action():
    r = reconcile(shop_refunded=1500.0, payment={"id": "pay_x"},
                  refunds=[{"id": "rfnd_a", "status": "failed", "amount": 1500.0}])
    assert r.verdict == "failed"
    assert "could not complete" in r.customer_safe_message
    assert "Re-issue" in r.action


def test_amount_mismatch_is_not_reported_as_confirmed():
    r = reconcile(shop_refunded=2450.0, payment={"id": "pay_x"},
                  refunds=[{"id": "rfnd_a", "status": "processed", "amount": 750.0}])
    assert r.verdict == "amount_mismatch"
    assert r.action


def test_no_claim_and_no_refund_is_unknown_not_confirmed():
    r = reconcile(shop_refunded=None, payment={"id": "pay_x"}, refunds=[])
    assert r.verdict == "unknown"


@pytest.mark.parametrize("verdict_case", [
    (2450.0, [], "never_issued"),
    (750.0, [{"status": "processed", "amount": 750.0, "id": "r"}], "confirmed"),
])
def test_no_verdict_ever_promises_money_it_cannot_prove(verdict_case):
    shop, refunds, expected = verdict_case
    r = reconcile(shop_refunded=shop, payment={"id": "p"}, refunds=refunds)
    assert r.verdict == expected
    if expected != "confirmed":
        msg = r.customer_safe_message.lower()
        assert "has been processed" not in msg
        assert "has been refunded" not in msg


# ------------------------------------------------------------- duplicates
def test_duplicate_capture_needs_two_actual_captures():
    one = [{"id": "p1", "status": "captured", "amount": 899.0},
           {"id": "p2", "status": "failed", "amount": 899.0}]
    assert detect_duplicate_capture(one) is None


def test_duplicate_capture_confirmed_and_quantified():
    two = [{"id": "p1", "status": "captured", "amount": 899.0, "currency": "INR"},
           {"id": "p2", "status": "captured", "amount": 899.0, "currency": "INR"}]
    out = detect_duplicate_capture(two)
    assert out["confirmed"] is True
    assert out["total_captured"] == 1798.0
    assert out["duplicate_amount"] == 899.0


# ------------------------------------------------------------ error shape
def test_missing_id_returns_400_but_maps_to_not_found():
    """Razorpay answers 400, not 404, for an id that does not exist."""
    resp = httpx.Response(400, json={"error": {
        "code": "BAD_REQUEST_ERROR",
        "description": "The id provided does not exist: pay_nope"}})
    assert isinstance(_map_error(resp), NotFound)


def test_authentication_failure_maps_to_auth_error():
    resp = httpx.Response(401, json={"error": {
        "code": "BAD_REQUEST_ERROR", "description": "Authentication failed",
        "reason": "authentication_failed"}})
    assert isinstance(_map_error(resp), AuthError)


# ------------------------------------------------------- end to end (mock)
async def test_get_payment_returns_rupees(service):
    p = await service.get_payment("pay_NrT4bM9cPqWxYz")
    assert p["amount"] == 2450.0          # 245000 paise
    assert p["status"] == "captured"
    assert p["bank_reference"] == "429798765432"


async def test_payment_with_no_refunds(service):
    out = await service.list_payment_refunds("pay_NrT4bM9cPqWxYz")
    assert out["count"] == 0


async def test_verify_refund_end_to_end_never_issued(service):
    out = await service.verify_refund("pay_NrT4bM9cPqWxYz", 2450.0)
    assert out["reconciliation"]["verdict"] == "never_issued"


async def test_verify_refund_end_to_end_confirmed(service):
    out = await service.verify_refund("pay_NuV2hI3jKlMnOp", 750.0)
    assert out["reconciliation"]["verdict"] == "confirmed"
    assert out["reconciliation"]["gateway_refunded"] == 750.0


async def test_verify_refund_end_to_end_failed(service):
    out = await service.verify_refund("pay_NzA9pQ1rStUvWx", 1500.0)
    assert out["reconciliation"]["verdict"] == "failed"


async def test_verify_duplicate_charge_end_to_end(service):
    out = await service.verify_duplicate_charge(
        ["pay_NsK1cD4eFgHiJk", "pay_NsK1dE5fGhIjKl"])
    assert out["duplicate_charge"]["confirmed"] is True
    assert out["duplicate_charge"]["duplicate_amount"] == 899.0


async def test_malformed_payment_id_rejected_before_any_request(service):
    for bad in ["", "nonsense", "pay_", "order_NqX8Z1pQrStUvW", "pay_<script>"]:
        with pytest.raises(InvalidRequest):
            await service.get_payment(bad)


async def test_duplicate_check_needs_at_least_two_ids(service):
    with pytest.raises(InvalidRequest):
        await service.verify_duplicate_charge(["pay_NsK1cD4eFgHiJk"])


async def test_status_reports_test_mode(service):
    st = await service.connector_status()
    assert st["connected"] is True
    assert st["razorpay"]["mode"] == "test"
    assert "warning" not in st


async def test_live_key_carries_a_warning():
    creds = Credentials("rzp_live_abc123", rzp_data.KEY_SECRET, "https://api.razorpay.test")
    client = RazorpayClient(creds, transport=httpx.ASGITransport(app=rzp_app),
                            rate_limiter=RateLimiter(10_000))
    st = await RazorpayService(client).connector_status()
    assert "LIVE" in st["warning"]
    await client.aclose()


# -------------------------------------------------------------- mcp layer
async def test_mcp_exposes_ten_read_only_tools():
    from razorpay_connector.mcp_server import ServiceProvider, build_server

    class Stub(ServiceProvider):
        async def get(self):
            client = RazorpayClient(CREDS, transport=httpx.ASGITransport(app=rzp_app),
                                    rate_limiter=RateLimiter(10_000))
            return RazorpayService(client)

    server = build_server(Stub())
    tools = await server.list_tools()
    assert {t.name for t in tools} == {
        # support: did the money move?
        "get_payment", "list_payment_refunds", "get_refund",
        "verify_refund", "verify_duplicate_charge",
        # finance: did the money arrive?
        "list_settlements", "get_settlement", "reconcile_settlement",
        "find_settlement_for_payment",
        "razorpay_status",
    }
    for t in tools:
        assert t.annotations.readOnlyHint is True
        assert t.description and len(t.description) > 60


# ------------------------------------------------------------- settlements
async def test_settlement_arithmetic_is_spelled_out(service):
    out = await service.get_settlement("setl_NmA1bCdEfGhIjK")
    assert out["net_amount_credited"] == 5442.96
    assert out["razorpay_fees"] == 87.32
    assert out["tax_on_fees"] == 15.72
    # gross is derived from net plus deductions, not taken on trust
    assert out["gross_before_deductions"] == 5546.0
    assert "= 5442.96 credited" in out["explanation"]["arithmetic"]


async def test_unprocessed_settlement_explains_the_missing_bank_line(service):
    """The most common 'our numbers do not match' explanation."""
    out = await service.get_settlement("setl_NmC3dEfGhIjKlM")
    assert out["status"] == "created"
    assert out["bank_reference"] is None
    assert "not been sent to the bank" in out["explanation"]["why_the_bank_may_not_show_it"]


async def test_processed_settlement_gives_the_bank_reference(service):
    out = await service.get_settlement("setl_NmA1bCdEfGhIjK")
    assert out["bank_reference"] == "KKBKH25092100451"
    assert "KKBKH25092100451" in out["explanation"]["how_to_find_it_on_the_statement"]


async def test_reconcile_settlement_lists_its_payments(service):
    out = await service.reconcile_settlement("setl_NmA1bCdEfGhIjK")
    assert out["payments_in_settlement"] == 4
    # 1298.00 + 2450.00 + 899.00 + 899.00
    assert out["gross_captured"] == 5546.0
    assert out["net_credited"] == 5442.96
    assert out["deducted"] == 103.04
    # the settlement agrees with its own payments, so nothing is unexplained
    assert "unexplained_difference" not in out


async def test_reconcile_names_disagreements_with_the_shop(service):
    """A payment that does not match the shop's order total is reported,
    not averaged away. Partial captures cause this routinely."""
    out = await service.reconcile_settlement(
        "setl_NmB2cDeFgHiJkL", shop_order_totals={"pay_NuV2hI3jKlMnOp": 3500.0})
    assert out["amount_mismatches"]
    row = out["amount_mismatches"][0]
    assert row["shop_order_total"] == 3500.0
    assert row["amount"] == 3200.0
    assert row["mismatch"] == 300.0
    assert "partial captures" in out["warning"].lower()


async def test_matching_totals_produce_no_warning(service):
    out = await service.reconcile_settlement(
        "setl_NmB2cDeFgHiJkL", shop_order_totals={"pay_NuV2hI3jKlMnOp": 3200.0})
    assert "amount_mismatches" not in out
    assert "warning" not in out


async def test_find_settlement_for_a_paid_out_payment(service):
    out = await service.find_settlement_for_payment("pay_NqX8aK2bLmTfQw")
    assert out["settled"] is True
    assert out["settlement"]["id"] == "setl_NmA1bCdEfGhIjK"


async def test_payment_in_an_unprocessed_settlement_is_not_settled(service):
    out = await service.find_settlement_for_payment("pay_NwX5kL6mNoPqRs")
    assert out["settled"] is False
    assert out["settlement"]["status"] == "created"


async def test_payment_in_no_settlement_says_so_without_guessing(service):
    out = await service.find_settlement_for_payment("pay_NzA9pQ1rStUvWx")
    assert out["settled"] is False
    assert out["settlement"] is None
    assert "settlement cycle" in out["note"]


async def test_settlements_flag_what_has_not_reached_the_bank(service):
    out = await service.list_settlements()
    assert out["not_yet_at_the_bank"]["count"] == 1
    assert "statement will not show" in out["not_yet_at_the_bank"]["note"]


async def test_unexplained_difference_is_surfaced_not_hidden(service, monkeypatch):
    """If the gateway's own numbers do not tie out, say so.

    Refunds netted into the same cycle are the usual cause, and reporting a
    shortfall without checking those is how a merchant gets told the wrong
    thing about their money.
    """
    from razorpay_connector.settlements import (
        normalize_settlement,
        reconcile_orders_to_settlement,
    )
    settlement = normalize_settlement(
        {"id": "setl_x", "status": "processed", "amount": 100000, "fees": 1000, "tax": 180})
    rows = [{"entity_id": "pay_a", "amount": 150000}]   # more captured than accounted for
    out = reconcile_orders_to_settlement(settlement, rows)
    assert out["unexplained_difference"] == 488.2
    assert "refunds netted off" in out["unexplained_note"].lower()


async def test_malformed_settlement_id_rejected(service):
    for bad in ["", "setl_", "pay_NqX8aK2bLmTfQw", "nonsense"]:
        with pytest.raises(InvalidRequest):
            await service.get_settlement(bad)
