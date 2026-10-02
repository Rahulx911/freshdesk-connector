"""Compare what the shop believes against what the gateway confirms.

This is the module the whole pair exists for. The WooCommerce connector can
say "a refund is recorded and I see no gateway id". Only Razorpay can say
whether money actually moved, and the answer has four distinct shapes that a
customer-facing agent must not blur together:

    confirmed       the gateway processed it; it is safe to tell the customer
    in_flight       the gateway accepted it but has not settled it yet
    failed          the gateway tried and it bounced; someone must act today
    never_issued    the shop thinks it refunded; the gateway has no record

`never_issued` is the dangerous one, and it is invisible from either system
alone. WooCommerce shows a refunded order with a net payment of zero.
Razorpay shows a captured payment with nothing refunded. Both are internally
consistent. Only together do they show a customer who was told their money
was coming and will never receive it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Razorpay returns money in paise. Every amount crossing this boundary is
# converted exactly once, here, so the factor-of-100 bug has one place to live.
PAISE_PER_RUPEE = 100


def paise_to_major(amount: Any) -> float | None:
    """245000 paise -> 2450.0 rupees."""
    try:
        return round(int(amount) / PAISE_PER_RUPEE, 2)
    except (TypeError, ValueError):
        return None


VERDICTS = ("confirmed", "in_flight", "failed", "never_issued", "amount_mismatch", "unknown")


@dataclass
class Reconciliation:
    verdict: str = "unknown"
    shop_refunded: float | None = None
    gateway_refunded: float | None = None
    currency: str = "INR"
    refunds: list[dict] = field(default_factory=list)
    summary: str = ""
    customer_safe_message: str = ""
    action: str | None = None

    def to_dict(self) -> dict:
        out: dict[str, Any] = {"verdict": self.verdict, "summary": self.summary,
                               "customer_safe_message": self.customer_safe_message}
        if self.shop_refunded is not None:
            out["shop_refunded"] = self.shop_refunded
        if self.gateway_refunded is not None:
            out["gateway_refunded"] = self.gateway_refunded
        out["currency"] = self.currency
        if self.refunds:
            out["gateway_refunds"] = self.refunds
        if self.action:
            out["action"] = self.action
        return out


def normalize_payment(raw: dict) -> dict:
    """Shrink a Razorpay payment and convert its amounts out of paise."""
    acquirer = raw.get("acquirer_data") or {}
    return {k: v for k, v in {
        "id": raw.get("id"),
        "status": raw.get("status"),
        "method": raw.get("method"),
        "captured": raw.get("captured"),
        "currency": raw.get("currency"),
        "amount": paise_to_major(raw.get("amount")),
        "amount_refunded": paise_to_major(raw.get("amount_refunded")),
        "refund_status": raw.get("refund_status"),
        "gateway_order_id": raw.get("order_id"),
        "created_at": raw.get("created_at"),
        "bank_reference": acquirer.get("rrn") or acquirer.get("upi_transaction_id"),
        "card_arn": acquirer.get("arn"),
    }.items() if v not in (None, "")}


def normalize_refund(raw: dict) -> dict:
    acquirer = raw.get("acquirer_data") or {}
    return {k: v for k, v in {
        "id": raw.get("id"),
        "status": raw.get("status"),
        "amount": paise_to_major(raw.get("amount")),
        "currency": raw.get("currency"),
        "payment_id": raw.get("payment_id"),
        "speed_processed": raw.get("speed_processed"),
        "created_at": raw.get("created_at"),
        "card_arn": acquirer.get("arn"),
    }.items() if v not in (None, "")}


def reconcile(*, shop_refunded: float | None, payment: dict, refunds: list[dict],
              currency: str = "INR") -> Reconciliation:
    """Decide the verdict from the shop's claim and the gateway's record.

    `shop_refunded` is what WooCommerce believes it refunded, in rupees.
    `payment` and `refunds` are already normalised out of paise.
    """
    rec = Reconciliation(shop_refunded=shop_refunded, currency=currency, refunds=refunds)
    processed = [r for r in refunds if r.get("status") == "processed"]
    pending = [r for r in refunds if r.get("status") == "pending"]
    failed = [r for r in refunds if r.get("status") == "failed"]
    rec.gateway_refunded = round(sum(r.get("amount") or 0 for r in processed), 2)

    claims_refund = bool(shop_refunded)

    if failed and not processed:
        rec.verdict = "failed"
        rec.summary = (f"Razorpay tried to refund {currency} "
                       f"{failed[0].get('amount')} and it failed.")
        rec.customer_safe_message = (
            "The refund was attempted but the payment provider could not complete it. "
            "A human is picking this up today and will come back to you."
        )
        rec.action = "Re-issue the refund in Razorpay, then tell the customer."
        return rec

    if pending and not processed:
        rec.verdict = "in_flight"
        rec.summary = f"Refund accepted by Razorpay, not settled yet ({pending[0].get('id')})."
        rec.customer_safe_message = (
            "Your refund has been sent to your bank and is still being processed. "
            "It usually lands within a few working days."
        )
        return rec

    if claims_refund and not refunds:
        rec.verdict = "never_issued"
        rec.summary = (
            f"The shop records a refund of {currency} {shop_refunded} but Razorpay has no "
            f"refund against payment {payment.get('id')}. The money never left the gateway."
        )
        rec.customer_safe_message = (
            "Your refund has been approved in our system but has not actually been sent yet. "
            "I am escalating this now so it goes out today, and you will get a confirmation."
        )
        rec.action = (
            f"Issue the refund in Razorpay against {payment.get('id')}. The customer may already "
            "have been told it was done."
        )
        return rec

    if processed:
        if shop_refunded is not None and abs((rec.gateway_refunded or 0) - shop_refunded) > 0.01:
            rec.verdict = "amount_mismatch"
            rec.summary = (f"The shop records {currency} {shop_refunded} refunded; Razorpay "
                           f"processed {currency} {rec.gateway_refunded}.")
            rec.customer_safe_message = (
                "There is a discrepancy between our records and the payment provider's. "
                "A human is checking the exact amount before I confirm anything."
            )
            rec.action = "Reconcile the amounts before replying to the customer."
            return rec
        rec.verdict = "confirmed"
        arn = next((r.get("card_arn") for r in processed if r.get("card_arn")), None)
        rec.summary = (f"Razorpay processed {currency} {rec.gateway_refunded} "
                       f"({', '.join(r['id'] for r in processed)}).")
        rec.customer_safe_message = (
            f"Your refund of {currency} {rec.gateway_refunded} has been processed by our payment "
            "provider." + (f" Your bank can trace it with reference {arn}." if arn else "")
        )
        return rec

    rec.verdict = "unknown"
    rec.summary = "No refund recorded in the shop and none at the gateway."
    rec.customer_safe_message = (
        "I cannot see a refund on this order. Let me check with a colleague before I say more."
    )
    return rec


def detect_duplicate_capture(payments: list[dict]) -> dict | None:
    """Two captured payments for the same order is a real double charge.

    The store side can only see that two payment ids are attached to one
    order, which a retry can cause without a second charge ever landing.
    Only the gateway can say both were actually captured.
    """
    captured = [p for p in payments if p.get("status") == "captured" and not p.get("amount_refunded")]
    if len(captured) < 2:
        return None
    total = round(sum(p.get("amount") or 0 for p in captured), 2)
    return {
        "confirmed": True,
        "captured_payments": [p["id"] for p in captured],
        "total_captured": total,
        "duplicate_amount": round(total - (captured[0].get("amount") or 0), 2),
        "currency": captured[0].get("currency", "INR"),
        "action": "Refund the duplicate capture in Razorpay and tell the customer.",
    }
