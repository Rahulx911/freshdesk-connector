"""Read-only primitives over the WooCommerce REST API.

Every method returns plain dicts ready to hand to a model: normalised,
PII-masked, guardrail-flagged and size-bounded. Nothing here issues anything
but a GET.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from . import query as q
from .client import WooClient
from .errors import InvalidRequest, NotFound
from .guardrails import fit_response, flag_record
from .insights import attention_score, derive
from .normalize import normalize_customer, normalize_order, normalize_product, normalize_refund


class WooService:
    def __init__(
        self,
        client: WooClient,
        *,
        redact_pii: bool = True,
        hpos_admin_links: bool = True,
    ):
        self.client = client
        self.redact_pii = redact_pii
        self.hpos = hpos_admin_links

    @property
    def store_url(self) -> str:
        return self.client.creds.store_url

    # ------------------------------------------------------------- helpers
    def _order(self, raw: dict, *, include_items: bool = True) -> dict:
        rec = normalize_order(
            raw,
            store_url=self.store_url,
            redact_pii=self.redact_pii,
            hpos=self.hpos,
            include_items=include_items,
        )
        return flag_record(rec, "customer_note")

    @staticmethod
    def _page_block(info: dict, params: dict) -> dict:
        page = params.get("page", 1)
        out = {"page": page, "per_page": params.get("per_page"), "has_more": info.get("has_more", False)}
        if info.get("total") is not None:
            out["total_matching"] = info["total"]
        if info.get("total_pages") is not None:
            out["total_pages"] = info["total_pages"]
        if out["has_more"]:
            out["next_page"] = page + 1
        return out

    # -------------------------------------------------------------- orders
    async def list_orders(self, **kwargs: Any) -> dict:
        params = q.build_order_query(**kwargs)
        data, info = await self.client.get_page("/orders", params)
        items = [self._order(o, include_items=False) for o in data or []]
        return fit_response({"items": items, **self._page_block(info, params)})

    async def search_orders(self, **kwargs: Any) -> dict:
        """Same endpoint as list_orders; a separate tool so the model is
        nudged to pass filters instead of paging blindly."""
        return await self.list_orders(**kwargs)

    async def get_order(self, order_id: int, *, max_refunds: int = 20) -> dict:
        oid = q.clean_id(order_id, "order_id")
        raw = await self.client.get_json(f"/orders/{oid}")
        if not isinstance(raw, dict) or not raw.get("id"):
            raise NotFound(f"Order {oid} not found")
        record = self._order(raw)
        refunds = raw.get("refunds") or []
        if refunds:
            # The order payload carries refund stubs; fetch the full rows so
            # the agent sees amounts and reasons, not just ids.
            full = await self.client.get_json(f"/orders/{oid}/refunds", {"per_page": max_refunds})
            rows = [normalize_refund(r) for r in (full or []) if isinstance(r, dict)]
            record["refunds"] = [flag_record(r, "reason") for r in rows]
        return record

    async def list_order_refunds(self, order_id: int, *, per_page: int | None = None,
                                 page: int | None = None) -> dict:
        oid = q.clean_id(order_id, "order_id")
        params = {"per_page": q.clean_per_page(per_page), "page": q.clean_page(page)}
        data, info = await self.client.get_page(f"/orders/{oid}/refunds", params)
        rows = [flag_record(normalize_refund(r), "reason") for r in (data or []) if isinstance(r, dict)]
        return fit_response({"order_id": oid, "items": rows, **self._page_block(info, params)})

    # ------------------------------------------------------------ products
    async def list_products(self, **kwargs: Any) -> dict:
        params = q.build_product_query(**kwargs)
        data, info = await self.client.get_page("/products", params)
        items = [normalize_product(p, store_url=self.store_url) for p in data or []]
        return fit_response({"items": items, **self._page_block(info, params)})

    async def get_product(self, product_id: int) -> dict:
        pid = q.clean_id(product_id, "product_id")
        raw = await self.client.get_json(f"/products/{pid}")
        if not isinstance(raw, dict) or not raw.get("id"):
            raise NotFound(f"Product {pid} not found")
        rec = normalize_product(raw, store_url=self.store_url)
        desc = (raw.get("short_description") or "").strip()
        if desc:
            rec["short_description"] = desc[:800]
        return rec

    # ----------------------------------------------------------- customers
    async def find_customers(self, **kwargs: Any) -> dict:
        params = q.build_customer_query(**kwargs)
        data, info = await self.client.get_page("/customers", params)
        items = [
            normalize_customer(c, store_url=self.store_url, redact_pii=self.redact_pii)
            for c in data or []
        ]
        return fit_response({"items": items, **self._page_block(info, params)})

    async def get_customer(self, customer_id: int) -> dict:
        cid = q.clean_id(customer_id, "customer_id")
        raw = await self.client.get_json(f"/customers/{cid}")
        if not isinstance(raw, dict) or not raw.get("id"):
            raise NotFound(f"Customer {cid} not found")
        return normalize_customer(raw, store_url=self.store_url, redact_pii=self.redact_pii)

    async def customer_order_history(
        self, *, customer_id: int | None = None, email: str | None = None, limit: int = 10
    ) -> dict:
        """Resolve a customer by id or email, then summarise their orders.

        Guest checkouts have `customer_id = 0`, so an email lookup falls back
        to searching orders directly. Without that fallback the agent would
        wrongly tell a guest customer it has no record of them.
        """
        if customer_id is None and not email:
            raise InvalidRequest("Pass customer_id or email")
        limit = q.clean_per_page(limit)
        resolved: dict | None = None

        if customer_id is not None:
            resolved = await self.get_customer(customer_id)
            params = q.build_order_query(customer_id=customer_id, per_page=limit, status=["any"])
        else:
            found = await self.find_customers(email=email, per_page=5)
            if found["items"]:
                resolved = found["items"][0]
                params = q.build_order_query(customer_id=resolved["id"], per_page=limit, status=["any"])
            else:
                # guest checkout: WooCommerce order search covers billing email
                params = q.build_order_query(search=email, per_page=limit, status=["any"])

        data, info = await self.client.get_page("/orders", params)
        orders = [self._order(o, include_items=False) for o in data or []]
        summary = _summarise_orders(orders)
        out: dict[str, Any] = {
            "customer": resolved,
            "matched_by": "customer_id" if customer_id is not None else ("account" if resolved else "guest_email"),
            "orders": orders,
            "summary": summary,
            **self._page_block(info, params),
        }
        if resolved is None:
            out["note"] = (
                "No customer account matched. These orders were matched on the order's billing "
                "details, which is how guest checkouts appear."
            )
        return fit_response(out, list_key="orders")

    # ---------------------------------------------------------- store pulse
    async def store_pulse(self, *, days: int = 14, scan: int = 100, top: int = 10) -> dict:
        """One call that answers 'what needs attention in the store today?'.

        Scans recent orders, derives signals locally, and ranks the ones that
        need a human, each with the reasons that produced the rank.
        """
        if days < 1 or days > 90:
            raise InvalidRequest("days must be between 1 and 90")
        scan = q.clean_per_page(scan)
        now = datetime.now(timezone.utc)
        after = (now.timestamp() - days * 86400)
        after_iso = datetime.fromtimestamp(after, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

        params = q.build_order_query(status=["any"], created_after=after_iso, per_page=scan)
        data, info = await self.client.get_page("/orders", params)
        raws = [o for o in (data or []) if isinstance(o, dict)]

        by_state: dict[str, int] = {}
        by_intent: dict[str, int] = {}
        by_gateway: dict[str, int] = {}
        gross = 0.0
        refunded = 0.0
        unreconciled: list[dict] = []
        ranked: list[dict] = []

        for raw in raws:
            sig = derive(raw, now=now)
            by_state[sig.payment_state] = by_state.get(sig.payment_state, 0) + 1
            if sig.intent:
                by_intent[sig.intent] = by_intent.get(sig.intent, 0) + 1
            if sig.gateway:
                by_gateway[sig.gateway] = by_gateway.get(sig.gateway, 0) + 1
            gross += (sig.amounts.get("order_total") or 0) or 0
            refunded += (sig.amounts.get("refunded_total") or 0) or 0

            score, why = attention_score(sig, raw)
            if score > 0:
                entry = {
                    "id": raw.get("id"),
                    "number": raw.get("number"),
                    "status": raw.get("status"),
                    "total": raw.get("total"),
                    "currency": raw.get("currency"),
                    "payment_state": sig.payment_state,
                    "intent": sig.intent,
                    "score": score,
                    "why": why,
                    "source_url": normalize_order(
                        raw, store_url=self.store_url, redact_pii=True, hpos=self.hpos,
                        include_items=False, signals=sig,
                    )["source_url"],
                }
                ranked.append(entry)
            if (sig.reconciliation or {}).get("status") == "refund_not_confirmed_at_gateway":
                unreconciled.append({"id": raw.get("id"), "number": raw.get("number"),
                                     "total": raw.get("total")})

        ranked.sort(key=lambda e: (-e["score"], e["id"] or 0))
        return fit_response({
            "window_days": days,
            "orders_scanned": len(raws),
            "orders_matching_window": info.get("total"),
            "scan_complete": not info.get("has_more", False),
            "gross_value": round(gross, 2),
            "refunded_value": round(refunded, 2),
            "by_payment_state": dict(sorted(by_state.items(), key=lambda kv: -kv[1])),
            "by_intent": dict(sorted(by_intent.items(), key=lambda kv: -kv[1])),
            "by_gateway": dict(sorted(by_gateway.items(), key=lambda kv: -kv[1])),
            "refunds_not_confirmed_at_gateway": unreconciled,
            "needs_attention": ranked[: max(1, min(top, 50))],
        }, list_key="needs_attention")

    # -------------------------------------------------------------- status
    async def connector_status(self) -> dict:
        creds = self.client.creds
        out: dict[str, Any] = {
            "connected": False,
            "store": creds.redacted(),
            "mode": "read-only (GET requests only; a Read-scoped WooCommerce key cannot write)",
            "pii_redaction": self.redact_pii,
            "admin_link_style": "hpos" if self.hpos else "legacy",
        }
        try:
            ping = await self.client.ping()
            out["connected"] = True
            out["orders_visible"] = ping.get("orders_visible")
        except Exception as exc:
            out["error"] = getattr(exc, "code", type(exc).__name__)
            out["message"] = str(exc)
        out["rate_budget"] = await self.client.rl.stats()
        return out


def _summarise_orders(orders: list[dict]) -> dict:
    total = 0.0
    states: dict[str, int] = {}
    refs: list[str] = []
    for o in orders:
        try:
            total += float(o.get("total") or 0)
        except (TypeError, ValueError):
            pass
        sig = o.get("signals") or {}
        state = sig.get("payment_state") or "unknown"
        states[state] = states.get(state, 0) + 1
        for pid in (sig.get("payment_refs") or {}).get("payment_id", []):
            if pid not in refs:
                refs.append(pid)
    return {k: v for k, v in {
        "orders": len(orders),
        "lifetime_value_in_window": round(total, 2) if orders else None,
        "by_payment_state": states or None,
        "razorpay_payment_ids": refs or None,
    }.items() if v is not None}
