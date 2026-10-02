# What the agent can and can't do

The deliverable the assignment asks for: an honest account of the connector's
reach, written for whoever has to decide whether to trust it with a merchant.

## What has and has not been verified

Stated plainly, because "it works" means different things for different parts
of this system.

| Component | Verified against | Status |
|---|---|---|
| WooCommerce connector | Real WordPress 7.1 + WooCommerce 11.1, in Docker and in CI | **Verified live** |
| WooCommerce auth, both flows | Real store: Basic over HTTPS, OAuth 1.0a over plain HTTP | **Verified live** |
| Read-only guarantee | Real store refused a hand-signed write: `401 The API key provided does not have write permissions` | **Verified live** |
| Admin deep links | Opened in a real wp-admin; resolved to the right order | **Verified live** |
| Rate-limit handling | Simulated 429 and 503 with `Retry-After`, not a real throttling host | Simulated |
| Razorpay connector | A mock gateway in this repository, **not a real Razorpay account** | **Not verified live** |
| Agent behaviour | 19 scenarios in oracle mode, deterministic scoring, no model in the loop | No model run |

### The Razorpay gap, specifically

The gateway half has never talked to Razorpay. Everything it does was built
against `mock_razorpay/`, which reproduces the real API's shapes from the
published documentation: HTTP Basic with the key id and secret, amounts in
paise, the `{"error": {...}}` envelope, the `{"entity": "collection", ...}`
wrapper on refund lists, and `400` rather than `404` for an id that does not
exist. Those are the details a connector gets wrong, and they are pinned in
`tests/test_razorpay.py`. But reproducing a documented contract is not the
same as meeting the real one, and the WooCommerce half taught me that
difference the hard way: the published documentation did not tell me that
Basic auth silently fails without TLS. Only a real store did.

**Why it is not closed:** a Razorpay account needs a merchant signup with
Indian business details, which is not something a take-home should require.

**How it would close:** the connector reads `RAZORPAY_BASE_URL`, which only
exists so the mock can be substituted. With real test-mode credentials the
verification is three environment variables and the existing demo, no code
change:

```bash
export RAZORPAY_BASE_URL=https://api.razorpay.com
export RAZORPAY_KEY_ID=rzp_test_...
export RAZORPAY_KEY_SECRET=...
```

Test-mode keys are free and describe no real money, so this is a credential
gap rather than a design one.

## Can

### Orders
- List recent orders, newest first, with WooCommerce's own total count.
- Search orders by status, free text (order number, billing name, billing
  email), created/modified date range, customer id or product id.
- Fetch one order in full: line items, totals, billing and shipping, the
  customer note, and the refund rows recorded against it.
- List the refund rows for an order on their own.

### Products and customers
- Find products by name, SKU, stock status or publication status; read price,
  sale price, stock level and lifetime sales.
- Find registered customers by name or email; read order count and spend.
- Pull a customer's order history by id **or** by email, including guest
  checkouts, which have no customer account at all.

### Payment intelligence (the part a generic reader does not have)
- Extract Razorpay references from every order: payment, gateway order,
  refund, subscription, plan, invoice, payment-link and settlement ids, plus
  labelled UPI UTR/RRN and card ARN values.
- Classify payment state: paid, awaiting_payment, failed, cancelled,
  partially_refunded, fully_refunded.
- Flag reconciliation gaps, which is the point of the connector:
  - `refund_not_confirmed_at_gateway` — WooCommerce says refunded, no `rfnd_`
    id exists, so the money may not have moved;
  - `multiple_payments` — more than one `pay_` id on one order;
  - `paid_without_gateway_reference` — paid via Razorpay with nothing to
    reconcile against a settlement.
- Rank the backlog with `store_pulse`, every entry carrying the plain-language
  reasons behind its score.

### Finance questions, with the paired Razorpay connector
- Explain a settlement: gross captured, Razorpay's fees, tax on those fees,
  the net credited, and the bank reference to find it on a statement.
- List which payments are inside a settlement and whether it ties out,
  cross-checked against the shop's own order totals when those are supplied.
- Say whether a given payment has been paid out yet, which is what finance
  needs when a charge is disputed.
- Explain the commonest "our numbers do not match": a settlement that has
  not reached the bank yet, so the statement correctly does not show it.

### With the paired Razorpay connector
- Resolve a payment reference into what the gateway actually did.
- Answer "did the refund reach the customer?" with one of five verdicts:
  `confirmed`, `in_flight`, `failed`, `never_issued`, `amount_mismatch`.
  Each carries a `customer_safe_message` phrased not to promise money that
  has not moved.
- Confirm or clear a suspected double charge. Two payment ids on one order is
  routine, because a retry makes one; two *captures* means the customer paid
  twice, and only the gateway can tell those apart.
- Report whether the key is test or live, because test-mode figures describe
  money that does not exist.

### Operationally
- Report its own health, the key's visibility and the remaining rate budget.
- Degrade predictably: structured JSON errors with an actionable `hint`,
  never a stack trace, never an unbounded wait.

## Can't

### By design
- **Write anything.** No refunds, no status changes, no cancellations, no
  notes, no customer edits. The connector issues only `GET`, and the
  WooCommerce key it uses is Read-scoped, so the store refuses writes
  independently of this code.
- **Re-issue a refund, or reverse a duplicate charge.** The Razorpay
  connector is read-only too. It names the action; a human performs it.
- **Reconcile without a payment reference.** An order paid by another gateway,
  or one where the plugin stored no `pay_` id, cannot be checked. The verdict
  is withheld rather than guessed.
- **Decide anything financial.** It will not tell a customer a refund has
  arrived. The prompts are written to stop exactly that.

### Limits of the data
- **Guest checkouts have no customer record.** `customer_order_history` falls
  back to matching on billing details and reports `matched_by: guest_email`.
  Two different people sharing an email address would collapse into one view.
- **`total_spent` and `orders_count` come from WooCommerce's own aggregates**
  and exclude guest orders placed before an account existed.
- **Stock figures are a snapshot.** They can change between the call and the
  customer reading the reply.
- **Responses may be up to 30 seconds stale.** A short cache keeps an agent
  from re-reading the same order three times in one conversation. Anything
  that must be current can bypass it.
- **Triage pages through the window, but not infinitely.** Past the page
  budget it reports lower bounds under different key names rather than
  totals, so a partial scan cannot be quoted as a complete one.
- **Refund reasons are free text** written by shop staff. A Razorpay refund id
  is only found there if someone pasted it.
- **Deleted and draft orders are invisible.** `trash` and `checkout-draft` are
  deliberately excluded from the allowed statuses.

### Limits of the deployment
- **One replica.** The rate budget and the response cache are in-process.
  Several replicas sharing one store would each think they had the full
  budget. The interface is shaped so a shared backend is a constructor
  change, but that backend is not built.
- **Hosted multi-merchant mode is built but not wired to a transport.**
  `tenancy.py` resolves a merchant from a hashed bearer token, server side,
  and `TenantServiceProvider` gives each merchant its own client, budget and
  cache. Over stdio there is no bearer token to read, so single-merchant mode
  is what actually runs today.
- **WooCommerce core only.** Subscriptions, Bookings, Memberships and most
  payment plugins expose their own REST namespaces that are not wired up. A
  Razorpay subscription is visible only through whatever it writes onto the
  order.
- **No webhooks.** The agent pulls; the store cannot push. Anything
  event-driven needs the webhook work described in ARCHITECTURE.md.

## Things that will surprise you about WooCommerce

Pinned as tests in `tests/test_api_conformance.py`, because each one is a
plausible implementation that is wrong against a real store.

| Behaviour | Consequence if you get it wrong |
|---|---|
| Basic auth works **only over HTTPS**; plain HTTP needs OAuth 1.0a | Every call returns `cannot_view`, which reads like a permissions bug, not an auth bug |
| Paging metadata is in `X-WP-Total` / `X-WP-TotalPages` / `Link`, not the body | You cannot tell the agent how many orders matched |
| `per_page` is capped at 100 | A 400 from the store instead of a clear client-side message |
| Guest orders have `customer_id = 0`, not `null` | A truthiness check assigns every guest order to customer 0 |
| Customers created at checkout have no WP role | The default role filter hides them and the agent reports "no account" |
| `woocommerce_rest_cannot_view` arrives as 401 on some hosts, 403 on others | A scoped-out key is misreported as a revoked one |
| WooCommerce 8.2+ moved orders out of `wp_posts` (HPOS) | The legacy `post.php` admin link 404s, so every citation is broken |
| A bare date in `after` is treated as midnight | Same-day orders silently disappear from the results |
| Razorpay returns amounts in **paise**, WooCommerce in rupees | Every figure quoted to a customer is wrong by a factor of 100 |
| Razorpay answers **400, not 404**, for an id that does not exist | A missing payment is reported as a malformed request |
