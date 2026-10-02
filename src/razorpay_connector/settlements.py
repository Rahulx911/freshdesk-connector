"""Settlement reconciliation: the finance team's weekly question.

Support asks "where is my refund?" daily. Finance asks something harder once
a week: *Razorpay says it sent us 4.12 lakh, the bank statement shows a
different number, which orders are in that transfer and where did the rest
go?*

That question is hard for a reason worth stating, because it is the first
thing to explain to a merchant:

* A settlement is **net**, not gross. Captured amount minus Razorpay's fees,
  minus tax on those fees, minus refunds netted off in the same cycle.
  Comparing an order total to a bank credit will never reconcile.
* Settlements lag. An order captured today lands in a transfer days later,
  so a date-aligned comparison is wrong by construction.
* A settlement in `created` state has no bank reference yet, so the bank
  genuinely does not show it. This is the single most common explanation for
  "the numbers do not match", and it is not a problem.

Everything here converts out of paise exactly once, through the same helper
the refund path uses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .reconcile import paise_to_major


@dataclass
class Settlement:
    id: str
    status: str
    net_amount: float | None
    fees: float | None
    tax: float | None
    gross_amount: float | None
    bank_reference: str | None
    created_at: int | None
    payments: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        out: dict[str, Any] = {
            "id": self.id,
            "status": self.status,
            "net_amount_credited": self.net_amount,
            "razorpay_fees": self.fees,
            "tax_on_fees": self.tax,
            "gross_before_deductions": self.gross_amount,
            "created_at": self.created_at,
        }
        if self.bank_reference:
            out["bank_reference"] = self.bank_reference
        else:
            out["bank_reference"] = None
            out["bank_reference_note"] = (
                "No bank reference yet. A settlement in this state has not reached the bank, "
                "so the statement will not show it. This is the usual reason a merchant's "
                "totals appear not to match."
            )
        if self.payments:
            out["payments"] = self.payments
            out["payment_count"] = len(self.payments)
        return {k: v for k, v in out.items() if v is not None or k == "bank_reference"}


def normalize_settlement(raw: dict) -> Settlement:
    net = paise_to_major(raw.get("amount"))
    fees = paise_to_major(raw.get("fees"))
    tax = paise_to_major(raw.get("tax"))
    gross = None
    if net is not None:
        gross = round(net + (fees or 0) + (tax or 0), 2)
    return Settlement(
        id=raw.get("id", ""),
        status=raw.get("status", "unknown"),
        net_amount=net,
        fees=fees,
        tax=tax,
        gross_amount=gross,
        bank_reference=raw.get("utr"),
        created_at=raw.get("created_at"),
    )


def explain_settlement(settlement: Settlement) -> dict:
    """Turn the arithmetic into something a finance team can check."""
    out: dict[str, Any] = {}
    if settlement.gross_amount is None or settlement.net_amount is None:
        return out
    out["arithmetic"] = (
        f"{settlement.gross_amount} captured "
        f"- {settlement.fees or 0} fees "
        f"- {settlement.tax or 0} tax "
        f"= {settlement.net_amount} credited"
    )
    if settlement.status != "processed":
        out["why_the_bank_may_not_show_it"] = (
            f"Settlement status is '{settlement.status}', not 'processed'. It has not been "
            "sent to the bank yet."
        )
    elif settlement.bank_reference:
        out["how_to_find_it_on_the_statement"] = (
            f"Look for bank reference {settlement.bank_reference}."
        )
    deduction = round((settlement.fees or 0) + (settlement.tax or 0), 2)
    if settlement.gross_amount:
        pct = round(deduction / settlement.gross_amount * 100, 2)
        out["deduction_rate"] = f"{deduction} of {settlement.gross_amount} ({pct}%)"
    return out


def reconcile_orders_to_settlement(
    settlement: Settlement,
    recon_rows: list[dict],
    shop_order_totals: dict[str, float] | None = None,
) -> dict:
    """Match the payments in a settlement back to the shop's own order totals.

    `shop_order_totals` maps a Razorpay payment id to what WooCommerce thinks
    that order was worth. Where the two disagree, say so rather than
    averaging them.
    """
    rows: list[dict] = []
    gross = 0.0
    mismatches: list[dict] = []

    for raw in recon_rows:
        amount = paise_to_major(raw.get("amount"))
        gross += amount or 0
        row = {
            "payment_id": raw.get("entity_id"),
            "amount": amount,
            "method": raw.get("method"),
        }
        if shop_order_totals:
            shop_total = shop_order_totals.get(str(raw.get("entity_id")))
            if shop_total is not None:
                row["shop_order_total"] = shop_total
                if amount is not None and abs(shop_total - amount) > 0.01:
                    row["mismatch"] = round(shop_total - amount, 2)
                    mismatches.append(row)
        rows.append(row)

    out: dict[str, Any] = {
        "settlement_id": settlement.id,
        "payments_in_settlement": len(rows),
        "gross_captured": round(gross, 2),
        "payments": rows,
    }
    if settlement.net_amount is not None:
        out["net_credited"] = settlement.net_amount
        out["deducted"] = round(gross - settlement.net_amount, 2)
    # Cross-check the gateway against itself. If the settlement's own net
    # does not equal the gross of its payments minus the deductions it
    # reports, something is unaccounted for: refunds netted into this cycle,
    # adjustments, or a payment missing from the report.
    if settlement.net_amount is not None and settlement.gross_amount is not None:
        unexplained = round(gross - settlement.gross_amount, 2)
        if abs(unexplained) > 0.01:
            out["unexplained_difference"] = unexplained
            out["unexplained_note"] = (
                f"The payments in this settlement total {round(gross, 2)}, but the settlement "
                f"accounts for {settlement.gross_amount} before deductions, a difference of "
                f"{unexplained}. Refunds netted off in the same cycle are the usual cause. "
                "Do not report a shortfall without checking those."
            )

    if mismatches:
        out["amount_mismatches"] = mismatches
        out["warning"] = (
            f"{len(mismatches)} payment(s) differ from the shop's order total. Partial "
            "captures and order edits after payment both cause this; check them before "
            "reporting a shortfall."
        )
    return out
