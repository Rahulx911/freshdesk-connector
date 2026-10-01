"""Payment signals for Razorpay merchants. Deterministic, local, no network.

Why this exists: a generic WooCommerce reader hands an agent an order with
`status: refunded` and nothing else. For a Razorpay merchant the interesting
question is whether the money actually moved, and the evidence for that is
scattered across `transaction_id`, the order's `meta_data`, and the refund
rows. This module pulls those together into one `signals` block.

Everything here is rule-based and runs in-process:

* no customer text is sent to a third-party model,
* the same order always produces the same signals,
* every `intent` carries `intent_evidence`, the literal field or phrase that
  triggered it, so a merchant can audit a routing decision.

The signal that earns its keep is `reconciliation`. WooCommerce records a
refund the moment a shop manager clicks refund. Whether Razorpay actually
sent the money is a *different* fact, recorded as a `rfnd_` id. An order
refunded in WooCommerce with no `rfnd_` reference is the exact case where a
customer is told "your refund is processed" and no money has moved.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# --------------------------------------------------------------- id patterns
# Razorpay ids are `<prefix>_<14+ base62>`. Anchored on a word boundary so a
# sentence like "we use pay_later" cannot match.
_RZP_PREFIXES = {
    "pay": "payment_id",
    "order": "gateway_order_id",
    "rfnd": "refund_id",
    "sub": "subscription_id",
    "plan": "plan_id",
    "inv": "invoice_id",
    "plink": "payment_link_id",
    "setl": "settlement_id",
    "qr": "qr_id",
}
_RZP_RE = re.compile(
    r"\b(" + "|".join(sorted(_RZP_PREFIXES, key=len, reverse=True)) + r")_([A-Za-z0-9]{10,24})\b"
)

# A bank reference is only a reference when something says so. A bare 12-digit
# number is just as likely to be a phone number or an order total in paise, so
# we require an adjacent label. This is the guard that keeps the
# false-positive corpus in tests/test_signals.py green.
_UTR_RE = re.compile(
    r"\b(?:utr|rrn|upi\s*(?:ref|txn)?|bank\s*ref(?:erence)?|ref(?:erence)?\s*no\.?)\s*[:#-]?\s*([A-Za-z0-9]{10,22})\b",
    re.IGNORECASE,
)
# Card refund ARN: labelled, 23 digits in practice but hosts vary.
_ARN_RE = re.compile(r"\barn\s*[:#-]?\s*(\d{12,25})\b", re.IGNORECASE)

# Razorpay-ish meta keys the official WooCommerce plugin writes.
_META_KEYS = {
    "_razorpay_payment_id": "payment_id",
    "razorpay_payment_id": "payment_id",
    "_razorpay_order_id": "gateway_order_id",
    "razorpay_order_id": "gateway_order_id",
    "_razorpay_refund_id": "refund_id",
    "razorpay_refund_id": "refund_id",
    "_razorpay_subscription_id": "subscription_id",
    "razorpay_subscription_id": "subscription_id",
    "_razorpay_signature": "signature_present",
}

_COD_HINTS = ("cod", "cash on delivery", "cheque", "bacs")
_RAZORPAY_HINTS = ("razorpay", "rzp")


@dataclass
class Signals:
    gateway: str | None = None
    payment_state: str = "unknown"
    payment_refs: dict[str, Any] = field(default_factory=dict)
    amounts: dict[str, Any] = field(default_factory=dict)
    reconciliation: dict[str, Any] = field(default_factory=dict)
    intent: str | None = None
    intent_evidence: str | None = None
    age: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        out: dict[str, Any] = {"payment_state": self.payment_state}
        if self.gateway:
            out["gateway"] = self.gateway
        if self.payment_refs:
            out["payment_refs"] = self.payment_refs
        if self.amounts:
            out["amounts"] = self.amounts
        if self.reconciliation:
            out["reconciliation"] = self.reconciliation
        if self.intent:
            out["intent"] = self.intent
            out["intent_evidence"] = self.intent_evidence
        if self.age:
            out["age"] = self.age
        return out


# ------------------------------------------------------------------ helpers
def _money(value: Any) -> float | None:
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _add_ref(refs: dict[str, Any], kind: str, value: str) -> None:
    bucket = refs.setdefault(kind, [])
    if value not in bucket:
        bucket.append(value)


def scan_text(text: str | None, refs: dict[str, Any] | None = None) -> dict[str, Any]:
    """Pull Razorpay ids and labelled bank references out of free text."""
    refs = refs if refs is not None else {}
    if not text:
        return refs
    for prefix, raw in _RZP_RE.findall(text):
        _add_ref(refs, _RZP_PREFIXES[prefix], f"{prefix}_{raw}")
    for utr in _UTR_RE.findall(text):
        if not utr.isalpha():          # a word after "ref:" is not a reference
            _add_ref(refs, "bank_reference", utr)
    for arn in _ARN_RE.findall(text):
        _add_ref(refs, "card_arn", arn)
    return refs


def _parse_dt(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _age_days(value: Any, now: datetime) -> float | None:
    dt = _parse_dt(value)
    if dt is None:
        return None
    return round((now - dt).total_seconds() / 86400.0, 1)


# -------------------------------------------------------------------- main
def derive(order: dict, *, now: datetime | None = None) -> Signals:
    """Build the signals block for one raw WooCommerce order."""
    now = now or datetime.now(timezone.utc)
    sig = Signals()
    refs: dict[str, Any] = {}

    method = (order.get("payment_method") or "").lower()
    title = (order.get("payment_method_title") or "").lower()
    blob = f"{method} {title}"
    if any(h in blob for h in _RAZORPAY_HINTS):
        sig.gateway = "razorpay"
    elif any(h in blob for h in _COD_HINTS):
        sig.gateway = "cash_on_delivery" if "cod" in blob or "cash" in blob else method or None
    elif method:
        sig.gateway = method

    # 1. the dedicated transaction_id field
    txn = (order.get("transaction_id") or "").strip()
    if txn:
        if _RZP_RE.fullmatch(txn):
            prefix = txn.split("_", 1)[0]
            _add_ref(refs, _RZP_PREFIXES[prefix], txn)
        else:
            _add_ref(refs, "transaction_id", txn)

    # 2. plugin meta_data
    for entry in order.get("meta_data") or []:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("key") or "")
        val = entry.get("value")
        mapped = _META_KEYS.get(key)
        if mapped == "signature_present":
            refs["signature_verified"] = bool(val)
            continue
        if mapped and isinstance(val, str) and val.strip():
            _add_ref(refs, mapped, val.strip())
        elif isinstance(val, str) and ("razorpay" in key.lower() or "rzp" in key.lower()):
            scan_text(val, refs)

    # 3. free text the merchant or customer wrote
    scan_text(order.get("customer_note"), refs)

    # 4. refund rows
    refunds = [r for r in (order.get("refunds") or []) if isinstance(r, dict)]
    refunded_total = 0.0
    for r in refunds:
        amt = _money(r.get("total"))
        if amt is not None:
            refunded_total += abs(amt)
        scan_text(r.get("reason"), refs)
        for entry in r.get("meta_data") or []:
            if isinstance(entry, dict) and isinstance(entry.get("value"), str):
                scan_text(entry["value"], refs)

    sig.payment_refs = refs

    # ------------------------------------------------------------- amounts
    currency = order.get("currency") or "INR"
    total = _money(order.get("total"))
    sig.amounts = {k: v for k, v in {
        "currency": currency,
        "order_total": total,
        "refunded_total": round(refunded_total, 2) if refunds else None,
        "net_total": round(total - refunded_total, 2) if total is not None and refunds else None,
    }.items() if v is not None}

    # -------------------------------------------------------- payment state
    status = (order.get("status") or "").lower()
    paid_at = order.get("date_paid")
    has_gateway_payment = bool(refs.get("payment_id"))

    if status == "failed":
        sig.payment_state = "failed"
    elif status == "cancelled":
        sig.payment_state = "cancelled"
    elif status == "refunded":
        sig.payment_state = "fully_refunded"
    elif refunds and total and refunded_total >= total - 0.01:
        sig.payment_state = "fully_refunded"
    elif refunds:
        sig.payment_state = "partially_refunded"
    elif status in ("pending", "on-hold"):
        sig.payment_state = "awaiting_payment"
    elif paid_at or has_gateway_payment or status in ("processing", "completed"):
        sig.payment_state = "paid"

    # ------------------------------------------------------ reconciliation
    gateway_refunds = refs.get("refund_id") or []
    recon: dict[str, Any] = {}
    if refunds:
        recon["woocommerce_refund_rows"] = len(refunds)
        recon["gateway_refund_ids"] = len(gateway_refunds)
        if not gateway_refunds:
            recon["status"] = "refund_not_confirmed_at_gateway"
            recon["explanation"] = (
                "WooCommerce records a refund but carries no Razorpay refund id. The money may "
                "not have left the gateway. Verify with the Razorpay Refunds API before telling "
                "the customer it is done."
            )
        elif len(gateway_refunds) < len(refunds):
            recon["status"] = "partially_confirmed"
            recon["explanation"] = "Fewer Razorpay refund ids than WooCommerce refund rows."
        else:
            recon["status"] = "confirmed"
    if sig.payment_state == "paid" and sig.gateway == "razorpay" and not has_gateway_payment:
        recon["status"] = "paid_without_gateway_reference"
        recon["explanation"] = (
            "Marked paid through Razorpay but no pay_ id was stored. Reconciliation against the "
            "Razorpay settlement will not be possible from this order alone."
        )
    if len(refs.get("payment_id") or []) > 1:
        recon["status"] = "multiple_payments"
        recon["explanation"] = (
            f"{len(refs['payment_id'])} Razorpay payment ids on one order; check for a double charge."
        )
    if recon:
        sig.reconciliation = recon

    # ---------------------------------------------------------------- age
    created_age = _age_days(order.get("date_created_gmt") or order.get("date_created"), now)
    modified_age = _age_days(order.get("date_modified_gmt") or order.get("date_modified"), now)
    sig.age = {k: v for k, v in {
        "days_since_created": created_age,
        "days_since_modified": modified_age,
    }.items() if v is not None}

    # ------------------------------------------------------------- intent
    sig.intent, sig.intent_evidence = _intent(sig, order, status, created_age)
    return sig


def _intent(sig: Signals, order: dict, status: str, age_days: float | None) -> tuple[str | None, str | None]:
    """One explainable intent per order, most actionable first."""
    recon_status = (sig.reconciliation or {}).get("status")

    if recon_status == "multiple_payments":
        return "double_charge", f"{len(sig.payment_refs['payment_id'])} Razorpay payment ids on one order"
    if recon_status == "refund_not_confirmed_at_gateway":
        return "refund_unconfirmed", "refund row present, no Razorpay rfnd_ id"
    if sig.payment_state == "failed":
        ref = (sig.payment_refs.get("payment_id") or ["none"])[0]
        return "payment_failed", f"order status 'failed' (gateway reference: {ref})"
    if sig.payment_state == "partially_refunded":
        return "partial_refund", f"refunded {sig.amounts.get('refunded_total')} of {sig.amounts.get('order_total')}"
    if sig.payment_state == "fully_refunded":
        return "refund_completed", "order status 'refunded'"
    if sig.payment_state == "awaiting_payment":
        if sig.gateway == "cash_on_delivery":
            return "cod_pending", f"cash on delivery, status '{status}'"
        detail = f"status '{status}'"
        if age_days is not None:
            detail += f", unpaid for {age_days} days"
        return "awaiting_payment", detail
    if recon_status == "paid_without_gateway_reference":
        return "unreconciled_payment", "paid via Razorpay with no pay_ id stored"
    if sig.payment_state == "paid":
        return "paid", f"status '{status}'" + (" with Razorpay reference" if sig.payment_refs.get("payment_id") else "")
    return None, None


# ------------------------------------------------------------ triage score
def attention_score(sig: Signals, order: dict) -> tuple[int, list[str]]:
    """Rank an order for the `store_pulse` needs-attention list.

    Scores are explainable: every point added appends a reason the merchant
    can read, which is what makes the ranking auditable rather than magic.
    """
    score = 0
    why: list[str] = []
    recon = (sig.reconciliation or {}).get("status")
    age = (sig.age or {}).get("days_since_created")
    total = (sig.amounts or {}).get("order_total") or 0

    if recon == "multiple_payments":
        score += 50
        why.append("possible double charge: more than one Razorpay payment id")
    if recon == "refund_not_confirmed_at_gateway":
        score += 40
        why.append("refund recorded in WooCommerce but not confirmed at Razorpay")
    if recon == "paid_without_gateway_reference":
        score += 15
        why.append("paid via Razorpay with no gateway reference stored")
    if sig.payment_state == "failed":
        score += 30
        why.append("payment failed")
    if sig.payment_state == "awaiting_payment":
        score += 15
        why.append("awaiting payment")
        if age is not None and age >= 3:
            score += 15
            why.append(f"unpaid for {age} days")
    if sig.intent == "partial_refund":
        score += 10
        why.append("partially refunded")
    if total >= 10000:
        score += 10
        why.append(f"high value order ({sig.amounts.get('currency', 'INR')} {total})")
    if sig.gateway == "cash_on_delivery" and total >= 5000:
        score += 10
        why.append("high value cash on delivery")
    return score, why
