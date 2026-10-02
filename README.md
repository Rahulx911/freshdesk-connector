# WooCommerce connector for Agent Studio

A production-grade, **read-only**, **payment-aware** connector that lets Agent Studio agents read a merchant's WooCommerce **orders, refunds, products and customers** through **MCP**. It covers Razorpay's Forward-Deployed Engineer assignment, Option 3.

## Why it's different: built for Razorpay merchants, not just for WooCommerce

A generic WooCommerce reader hands an agent an order marked `refunded` and leaves it there. For a Razorpay merchant the question that matters is **whether the money actually moved**, and the evidence for that is scattered across `transaction_id`, the order's `meta_data` and the refund rows. This connector pulls it together.

| | What the agent gets | Why it matters |
|---|---|---|
| **Payment references** | `signals.payment_refs`: Razorpay `pay_` / `order_` / `rfnd_` / `sub_` / `plink_`… ids, labelled UPI UTR/RRN, card ARN, pulled from `transaction_id`, plugin `meta_data`, customer notes and refund reasons | The agent can call Razorpay's Payments/Refunds APIs and answer "where is my refund?" from the source of truth |
| **Reconciliation** | `signals.reconciliation`: the gap between what WooCommerce believes and what the gateway confirms | **The signal this connector exists for.** A WooCommerce refund row only proves a shop manager clicked refund. Without a `rfnd_` id the money may never have left Razorpay, and the customer has been told it has |
| **Double charge detection** | Two `pay_` ids on one order raises `multiple_payments` | The single most expensive support ticket a D2C merchant gets |
| **Explainable intent** | `signals.intent` (refund_unconfirmed, double_charge, payment_failed, awaiting_payment, cod_pending…) **with the evidence that triggered it** | Routing the merchant can audit. No customer text is sent to a third-party model |
| **One-call triage** | `store_pulse`: the recent order mix by payment state, intent and gateway, plus a ranked `needs_attention` list **with reasons** | The question a shop manager asks every morning, answered in one call |
| **Citations** | `source_url` deep link on every record, HPOS-aware | Answers can be checked in wp-admin with one click |

### WooCommerce agrees with us

This is not a theory about how merchants get refunds wrong. When a shop manager marks an order refunded, WooCommerce writes this note on the order itself:

> Order status set to refunded. To return funds to the customer you will need to issue a refund through your payment gateway.

The order then reads `status: refunded`, `Refunded: -₹2,450.00`, `Net Payment: ₹0.00`. A connector that reports the status alone tells the agent the customer has their money. WooCommerce is saying the opposite in a note nobody reads, and `signals.reconciliation` is what surfaces it:

```json
{"status": "refund_not_confirmed_at_gateway",
 "woocommerce_refund_rows": 1, "gateway_refund_ids": 0,
 "explanation": "WooCommerce records a refund but carries no Razorpay refund id. The money may not have left the gateway. Verify with the Razorpay Refunds API before telling the customer it is done."}
```

Captured from the live local store, order 18.

Example `store_pulse` entry, from the live local store:

```json
{"id": 20, "number": "20", "status": "processing", "total": "899.00",
 "payment_state": "paid", "intent": "double_charge", "score": 50,
 "why": ["possible double charge: more than one Razorpay payment id"],
 "source_url": "http://localhost:8080/wp-admin/admin.php?page=wc-orders&action=edit&id=20"}
```

## Read-only, enforced by the credential

The connector only ever issues `GET`, and every tool is `readOnlyHint=True`. But the real guarantee is one level down: WooCommerce scopes each API key to Read, Write or Read-Write at creation, and this connector asks for a **Read** key. Even a bug in this repository cannot write to the store:

```
$ POST /wp-json/wc/v3/orders   (with the connector's key)
401  woocommerce_rest_authentication_error
     "The API key provided does not have write permissions."
```

That is the store refusing, not us.

## Try it in two minutes

**Against the bundled mock store**, no Docker, no accounts:

```bash
python -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"
python scripts/demo.py        # 26/26 checks
python -m evals.run --oracle  # 19/19 agent scenarios, no API key needed
pytest -q                     # 184 tests
```

**Against a real WooCommerce**, also with no accounts, because WooCommerce is self-hostable:

```bash
cd deploy/woo-local && docker compose up -d && ./bootstrap.sh
# prints a Read-scoped key; then, from the repo root:
export WOO_STORE_URL=http://localhost:8080
export WOO_CONSUMER_KEY=ck_...  WOO_CONSUMER_SECRET=cs_...
python scripts/demo.py --live  # 15/15 checks against WordPress 7.1 + WooCommerce 11.1
```

This is the reason WooCommerce was chosen: the whole thing is verifiable end to end on a laptop, with no trial, no credit card and no vendor account.

## What's in it

| Assignment asks for | Where |
|---|---|
| OAuth **or** API-key auth flow | **Both.** HTTP Basic with a consumer key/secret over HTTPS, and full **OAuth 1.0a one-legged signing** for plain-HTTP stores (`oauth.py`), because that is the only thing WooCommerce accepts without TLS. Keys verified at `auth login`, stored `0600`, or taken from the environment |
| list / get / search primitives | 11 tools: `list_orders`, `search_orders`, `get_order`, `list_order_refunds`, `list_products`, `get_product`, `find_customers`, `get_customer`, `customer_order_history`, `store_pulse`, `connector_status`, plus 2 MCP prompts |
| Rate-limit handling | WooCommerce core ships **no** rate limiter, so the budget is enforced client side over a sliding 60s window with a 20% reserve, and raised only when the store advertises a real limit. `429` and `503` both honour `Retry-After`; waits are bounded and fail fast with `retry_after_seconds` |
| MCP tool specification | `mcp_server.py`; exported JSON in [`docs/mcp_tool_spec.json`](docs/mcp_tool_spec.json) |
| What the agent can and can't do | [`docs/CAPABILITIES.md`](docs/CAPABILITIES.md) |
| How I'd roll this out with a merchant | [`docs/FDE_PLAYBOOK.md`](docs/FDE_PLAYBOOK.md) |

## Layout

```
src/woocommerce_connector/
  auth.py          consumer key/secret, 0600 store, URL validation (https only, no IPs/internal hosts)
  oauth.py         OAuth 1.0a one-legged signing, matching WooCommerce's own implementation
  ratelimit.py     client-side sliding-window request budget with reserve
  client.py        httpx: budget -> GET -> settle; 429/503 Retry-After, 5xx backoff, redirect refusal
  query.py         typed filters -> wc/v3 params (the model never writes query syntax)
  service.py       the 11 primitives
  normalize.py     LLM-shaped records, PII masking, HPOS-aware source_url
  insights.py      Razorpay refs / reconciliation / intent (deterministic, local, no network)
  guardrails.py    prompt-injection flags, response size budget (binary search)
  observability.py JSON logs, audit line per tool call (PII-masked), Prometheus metrics
  mcp_server.py    11 tools + 2 prompts, _call wrapper
  cli.py           auth login|status|logout, serve, export-spec
deploy/woo-local/  a real WordPress + WooCommerce store in Docker, seeded, with a read-only key
mock_server/       FastAPI WooCommerce test double (fictional "Kettle & Leaf" data)
evals/             19 agent scenarios + harness (oracle mode needs no API key)
tests/             184 tests including Hypothesis fuzzing and API conformance
docs/              CAPABILITIES, ARCHITECTURE, SECURITY, RUNBOOK, TESTING, FDE_PLAYBOOK, mcp_tool_spec.json
```

## Honest limitations

Read [`docs/CAPABILITIES.md`](docs/CAPABILITIES.md) for the full list. The short version:

- **Read-only.** No refunds, no status changes, no cancellations. Those need a human.
- **Single replica.** The rate budget is in-process. Several replicas against one store need a shared backend.
- **WooCommerce core only.** Subscriptions and Bookings expose their own endpoints that are not wired up.
- **The Razorpay side is not called.** The connector finds the references; pairing it with a Razorpay Payments/Refunds tool is the obvious next step and is what makes `reconciliation` actionable rather than advisory.
