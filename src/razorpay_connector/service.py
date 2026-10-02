"""Read-only Razorpay primitives, plus the cross-system verdict."""

from __future__ import annotations

import re
from typing import Any

from woocommerce_connector.errors import InvalidRequest

from .client import RazorpayClient
from .reconcile import (
    Reconciliation,
    detect_duplicate_capture,
    normalize_payment,
    normalize_refund,
    reconcile,
)
from .settlements import (
    explain_settlement,
    normalize_settlement,
    reconcile_orders_to_settlement,
)

_PAY_ID = re.compile(r"^pay_[A-Za-z0-9]{10,24}$")
_RFND_ID = re.compile(r"^rfnd_[A-Za-z0-9]{10,24}$")
_SETL_ID = re.compile(r"^setl_[A-Za-z0-9]{10,24}$")


def _check(value: str, pattern: re.Pattern, field: str, example: str) -> str:
    v = (value or "").strip()
    if not pattern.match(v):
        raise InvalidRequest(
            f"{field}={value!r} is not a Razorpay id",
            details={"expected": f"like {example}"},
        )
    return v


class RazorpayService:
    def __init__(self, client: RazorpayClient):
        self.client = client

    async def get_payment(self, payment_id: str) -> dict:
        pid = _check(payment_id, _PAY_ID, "payment_id", "pay_NqX8aK2bLmTfQw")
        return normalize_payment(await self.client.get(f"/payments/{pid}"))

    async def list_payment_refunds(self, payment_id: str) -> dict:
        pid = _check(payment_id, _PAY_ID, "payment_id", "pay_NqX8aK2bLmTfQw")
        body = await self.client.get(f"/payments/{pid}/refunds")
        items = [normalize_refund(r) for r in (body or {}).get("items", [])]
        return {"payment_id": pid, "count": len(items), "items": items}

    async def get_refund(self, refund_id: str) -> dict:
        rid = _check(refund_id, _RFND_ID, "refund_id", "rfnd_NuV2zY9xWvUtSr")
        return normalize_refund(await self.client.get(f"/refunds/{rid}"))

    async def verify_refund(self, payment_id: str, shop_refunded_amount: float | None = None) -> dict:
        """The cross-system answer: did the money actually move?

        Pass the amount WooCommerce believes it refunded (from
        `signals.amounts.refunded_total`) and this reports a verdict plus a
        message that is safe to send a customer.
        """
        pid = _check(payment_id, _PAY_ID, "payment_id", "pay_NqX8aK2bLmTfQw")
        payment = await self.get_payment(pid)
        refunds = (await self.list_payment_refunds(pid))["items"]
        amount: float | None = None
        if shop_refunded_amount is not None:
            try:
                amount = round(float(shop_refunded_amount), 2)
            except (TypeError, ValueError) as e:
                raise InvalidRequest(
                    f"shop_refunded_amount={shop_refunded_amount!r} is not a number") from e
        rec: Reconciliation = reconcile(
            shop_refunded=amount, payment=payment, refunds=refunds,
            currency=payment.get("currency", "INR"),
        )
        return {"payment": payment, "reconciliation": rec.to_dict()}

    async def verify_duplicate_charge(self, payment_ids: list[str]) -> dict:
        """Confirm or clear a suspected double charge.

        The store can only see two payment ids on one order, which a retry
        produces routinely. This says whether both were actually captured.
        """
        if not payment_ids or len(payment_ids) < 2:
            raise InvalidRequest("Pass at least two payment ids to compare")
        if len(payment_ids) > 10:
            raise InvalidRequest("Pass at most 10 payment ids")
        payments = [await self.get_payment(p) for p in payment_ids]
        verdict = detect_duplicate_capture(payments)
        out: dict[str, Any] = {"payments": payments}
        if verdict:
            out["duplicate_charge"] = verdict
            out["customer_safe_message"] = (
                f"You were charged twice, {verdict['currency']} {verdict['duplicate_amount']} more "
                "than you should have been. I am arranging the refund of the duplicate now."
            )
        else:
            out["duplicate_charge"] = {"confirmed": False}
            out["customer_safe_message"] = (
                "I can see more than one payment attempt, but only one actually went through, "
                "so you have been charged once."
            )
        return out

    # ---------------------------------------------------------- settlements
    async def list_settlements(self, limit: int = 10) -> dict:
        """Recent transfers to the merchant's bank, newest first."""
        if limit < 1 or limit > 100:
            raise InvalidRequest("limit must be between 1 and 100")
        body = await self.client.get("/settlements")
        items = [normalize_settlement(r).to_dict() for r in (body or {}).get("items", [])]
        unsettled = [s for s in items if s.get("status") != "processed"]
        out: dict[str, Any] = {"count": len(items), "items": items[:limit]}
        if unsettled:
            out["not_yet_at_the_bank"] = {
                "count": len(unsettled),
                "note": ("These have not reached the bank, so the statement will not show "
                         "them. Usually the reason a merchant's totals look short."),
            }
        return out

    async def get_settlement(self, settlement_id: str) -> dict:
        """One settlement, with the arithmetic spelled out."""
        sid = _check(settlement_id, _SETL_ID, "settlement_id", "setl_NmA1bCdEfGhIjK")
        settlement = normalize_settlement(await self.client.get(f"/settlements/{sid}"))
        out = settlement.to_dict()
        out["explanation"] = explain_settlement(settlement)
        return out

    async def reconcile_settlement(
        self, settlement_id: str, shop_order_totals: dict[str, float] | None = None
    ) -> dict:
        """Which payments are in this transfer, and does it tie out?

        Pass `shop_order_totals` mapping Razorpay payment ids to what the shop
        believes each order was worth, taken from the WooCommerce connector,
        and any disagreement is named rather than smoothed over.
        """
        sid = _check(settlement_id, _SETL_ID, "settlement_id", "setl_NmA1bCdEfGhIjK")
        settlement = normalize_settlement(await self.client.get(f"/settlements/{sid}"))
        body = await self.client.get("/settlements/recon/combined", {"settlement_id": sid})
        rows = (body or {}).get("items", [])
        result = reconcile_orders_to_settlement(settlement, rows, shop_order_totals)
        result["settlement"] = settlement.to_dict()
        result["explanation"] = explain_settlement(settlement)
        return result

    async def find_settlement_for_payment(self, payment_id: str) -> dict:
        """Which transfer did this payment land in, if any?

        The question a merchant asks when a customer disputes a charge and
        finance needs to know whether that money has already been paid out.
        """
        pid = _check(payment_id, _PAY_ID, "payment_id", "pay_NqX8aK2bLmTfQw")
        body = await self.client.get("/settlements/recon/combined")
        for row in (body or {}).get("items", []):
            if row.get("entity_id") == pid:
                sid = row.get("settlement_id")
                settlement = normalize_settlement(await self.client.get(f"/settlements/{sid}"))
                return {
                    "payment_id": pid,
                    "settled": settlement.status == "processed",
                    "settlement": settlement.to_dict(),
                    "explanation": explain_settlement(settlement),
                }
        return {
            "payment_id": pid,
            "settled": False,
            "settlement": None,
            "note": ("This payment is not in any settlement yet. Either it is within the "
                     "settlement cycle, or it was refunded before payout."),
        }

    async def connector_status(self) -> dict:
        creds = self.client.creds
        out: dict[str, Any] = {
            "connected": False,
            "razorpay": creds.redacted(),
            "mode": "read-only (GET requests only)",
        }
        if creds.mode == "live":
            out["warning"] = "This is a LIVE Razorpay key. Answers reflect real customer money."
        try:
            # Any well-formed id proves auth; a missing one still returns a
            # Razorpay-shaped error rather than an auth failure.
            await self.client.get("/payments/pay_connectivitycheck")
            out["connected"] = True
        except Exception as exc:
            code = getattr(exc, "code", type(exc).__name__)
            out["connected"] = code == "not_found"
            if not out["connected"]:
                out["error"] = code
                out["message"] = str(exc)
        out["rate_budget"] = await self.client.rl.stats()
        return out
