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

### What a plain connector answers, and what this one answers

Same order, same customer question, same moment.

| | A plain WooCommerce reader | This pair |
|---|---|---|
| Reads | `status: refunded`, `net payment: 0` | the same, plus Razorpay has no refund against the payment |
| Tells the customer | "Your refund has been processed." | "Your refund has been approved in our system but has not actually been sent yet. I am escalating this now." |
| Tells the merchant | nothing | "Issue the refund in Razorpay against `pay_NrT4bM9cPqWxYz`. The customer may already have been told it was done." |
| Outcome | a customer waiting for money that is not coming, and a second angry contact in a week | the refund goes out today |

The plain answer is not a bug in the reader. It is a faithful report of what WooCommerce says. The money is simply not in WooCommerce.

### Two connectors, one grounded answer

```bash
python scripts/demo_reconcile.py     # 11/11 checks
```

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
python scripts/demo.py            # 26/26 checks
python scripts/demo_reconcile.py  # 11/11, the two-connector story
python -m evals.run --oracle  # 19/19 agent scenarios, no API key needed
pytest -q                     # 210 tests
```

**Against a real WooCommerce**, also with no accounts, because WooCommerce is self-hostable:

```bash
cd deploy/woo-local
docker compose up -d && ./bootstrap.sh   # prints a Read-scoped key

cd ../..                                  # the scripts live at the repo root
export WOO_STORE_URL=http://localhost:8080
export WOO_CONSUMER_KEY=ck_...            # the real values bootstrap printed
export WOO_CONSUMER_SECRET=cs_...
python scripts/demo.py --live             # 15/15 against WordPress 7.1 + WooCommerce 11.1
python scripts/assert_read_only.py        # the store itself refuses a write
```

Re-running `bootstrap.sh` is safe: it detects an already-seeded store and does nothing.

This is the reason WooCommerce was chosen: the whole thing is verifiable end to end on a laptop, with no trial, no credit card and no vendor account.

## What's in it

| Assignment asks for | Where | Status |
|---|---|---|
| A connector an agent can use to read orders | 11 read-only MCP tools over `wc/v3` | done |
| Working OAuth **or** API-key auth flow | **Both.** HTTP Basic over HTTPS, and OAuth 1.0a one-legged signing for plain-HTTP stores (`oauth.py`) | done |
| Suitable list / get / search primitives | `list_orders`, `search_orders`, `get_order`, `list_order_refunds`, `list_products`, `get_product`, `find_customers`, `get_customer`, `customer_order_history` | done |
| Rate-limit handling | Client-side sliding budget with reserve, `429`/`503` `Retry-After`, bounded waits that fail fast with `retry_after_seconds` | done |
| MCP tool specification | [`docs/mcp_tool_spec.json`](docs/mcp_tool_spec.json), checked against the code by a test | done |
| Short doc on what the agent can and cannot do | [`docs/CAPABILITIES.md`](docs/CAPABILITIES.md) | done |

**Beyond the brief:** a paired Razorpay connector that resolves the references, a real WooCommerce store in Docker so everything is verifiable without an account, a 19-scenario eval harness, and a rollout playbook. See below.


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
src/razorpay_connector/
  auth.py          key id/secret, test-vs-live mode read from the key itself
  client.py        Razorpay REST client, same budget and backoff discipline
  reconcile.py     the cross-system verdict: confirmed / in_flight / failed / never_issued
  service.py       5 primitives + verify_refund + verify_duplicate_charge
  mcp_server.py    6 read-only tools
mock_server/       FastAPI WooCommerce test double (fictional "Kettle & Leaf" data)
mock_razorpay/     FastAPI Razorpay test double (amounts in paise, as the real API)
evals/             19 agent scenarios + harness (oracle mode needs no API key)
tests/             210 tests including Hypothesis fuzzing and API conformance
docs/              CAPABILITIES, ARCHITECTURE, SECURITY, RUNBOOK, TESTING,
                   FDE_PLAYBOOK, DESIGN_RATIONALE, mcp_tool_spec.json
```

## Honest limitations

Read [`docs/CAPABILITIES.md`](docs/CAPABILITIES.md) for the full list. The short version:

- **Read-only.** No refunds, no status changes, no cancellations. Those need a human.
- **Single replica.** The rate budget is in-process. Several replicas against one store need a shared backend.
- **WooCommerce core only.** Subscriptions and Bookings expose their own endpoints that are not wired up.
- **The Razorpay connector has never talked to Razorpay.** It was built against a mock that reproduces the documented API, and the WooCommerce half taught me that a documented contract and a real one are not the same thing. A merchant signup is not something a take-home should need. See [what has and has not been verified](docs/CAPABILITIES.md#what-has-and-has-not-been-verified). `scripts/verify_razorpay_contract.py` closes it against a free test-mode key, with no data needed in the account.
- **No write path anywhere.** Re-issuing a failed refund is named as an action for a human, never performed.
