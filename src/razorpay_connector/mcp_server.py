"""MCP server for the Razorpay half: 5 read-only tools.

Deliberately small. This is not a Razorpay connector in general; it is the
minimum needed to answer "did the money actually move?" about an order the
WooCommerce connector has already found.
"""

from __future__ import annotations

import inspect
import json

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from woocommerce_connector.errors import WooError
from woocommerce_connector.observability import audit, configure_logging

from .auth import Credentials, load
from .client import RazorpayClient
from .service import RazorpayService

SERVER_NAME = "razorpay-connector"

INSTRUCTIONS = inspect.cleandoc("""
    Read-only access to a Razorpay merchant account, for confirming what the
    payment gateway actually did.

    Use this with the WooCommerce connector, not instead of it. The store
    says what it believes; this says what happened to the money.

    * When a WooCommerce order shows `reconciliation.status` of
      `refund_not_confirmed_at_gateway`, call `verify_refund` with the
      `pay_` id and the shop's refunded amount. Do not answer the customer
      before you have.
    * When it shows `multiple_payments`, call `verify_duplicate_charge` with
      the payment ids. Two ids on an order is routine; two *captures* is a
      real double charge.
    * Every verdict comes with `customer_safe_message`. Prefer it over
      writing your own. It is phrased not to promise money that has not
      moved.
    * A `never_issued` verdict means the customer may already have been told
      a refund was sent when it was not. Say it is being escalated, give no
      date, and hand to a human.
    * Amounts here are in rupees. The underlying API uses paise; the
      conversion has already been done.
""")


class ServiceProvider:
    def __init__(self, creds: Credentials | None = None):
        self._creds = creds
        self._service: RazorpayService | None = None

    async def get(self) -> RazorpayService:
        if self._service is None:
            self._service = RazorpayService(RazorpayClient(self._creds or load()))
        return self._service

    async def aclose(self) -> None:
        if self._service is not None:
            await self._service.client.aclose()
            self._service = None


def build_server(provider: ServiceProvider | None = None) -> FastMCP:
    configure_logging()
    provider = provider or ServiceProvider()
    mcp = FastMCP(SERVER_NAME, instructions=INSTRUCTIONS)

    async def _call(tool: str, args: dict, fn) -> str:
        try:
            with audit(tool, args):
                service = await provider.get()
                result = await fn(service)
        except WooError as e:
            return json.dumps(e.to_dict())
        except Exception as e:
            return json.dumps({
                "error": "internal_error",
                "message": f"{type(e).__name__}: {e}",
                "hint": "This is a connector bug. Tell the user; do not retry.",
            })
        return json.dumps(result, default=str)

    ro = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True)

    def tool(**kwargs):
        def decorate(fn):
            if fn.__doc__:
                fn.__doc__ = inspect.cleandoc(fn.__doc__)
            return mcp.tool(**kwargs)(fn)
        return decorate

    @tool(annotations=ro)
    async def get_payment(payment_id: str) -> str:
        """Look up one Razorpay payment by its `pay_` id.

        Returns status, method, amount in rupees, how much has been refunded,
        and the bank reference or card ARN a customer's bank can trace.
        """
        return await _call("get_payment", locals(), lambda s: s.get_payment(payment_id))

    @tool(annotations=ro)
    async def list_payment_refunds(payment_id: str) -> str:
        """List every refund Razorpay holds against one payment.

        An empty list against an order the shop marked refunded is the
        signal that matters: the money never left the gateway.
        """
        return await _call("list_payment_refunds", locals(),
                           lambda s: s.list_payment_refunds(payment_id))

    @tool(annotations=ro)
    async def get_refund(refund_id: str) -> str:
        """Look up one refund by its `rfnd_` id.

        Status is the field to read: `processed` means the money has gone,
        `pending` means it has not yet, `failed` means it bounced and a human
        must re-issue it.
        """
        return await _call("get_refund", locals(), lambda s: s.get_refund(refund_id))

    @tool(annotations=ro)
    async def verify_refund(payment_id: str, shop_refunded_amount: float | None = None) -> str:
        """Did the refund the shop recorded actually reach the customer?

        This is the tool to reach for whenever a WooCommerce order says
        refunded. Pass the `pay_` id and the shop's refunded amount from
        `signals.amounts.refunded_total`.

        The verdict is one of: `confirmed` (safe to tell the customer),
        `in_flight` (accepted, not settled), `failed` (bounced, act today),
        `never_issued` (the shop thinks it refunded and the gateway has no
        record), or `amount_mismatch`. Use `customer_safe_message` rather
        than writing your own wording.
        """
        return await _call("verify_refund", locals(),
                           lambda s: s.verify_refund(payment_id, shop_refunded_amount))

    @tool(annotations=ro)
    async def verify_duplicate_charge(payment_ids: list[str]) -> str:
        """Was a suspected double charge real?

        Pass the payment ids the store attached to one order. Two ids is
        routine, because a retry creates one. Two *captures* means the
        customer paid twice. Only the gateway can tell these apart.
        """
        return await _call("verify_duplicate_charge", locals(),
                           lambda s: s.verify_duplicate_charge(payment_ids))

    @tool(annotations=ro)
    async def razorpay_status() -> str:
        """Check the Razorpay connection and whether the key is test or live.

        Read the mode before quoting any figure to a customer. Test-mode data
        describes money that does not exist.
        """
        return await _call("razorpay_status", {}, lambda s: s.connector_status())

    return mcp


def main() -> None:
    build_server().run()


if __name__ == "__main__":
    main()
