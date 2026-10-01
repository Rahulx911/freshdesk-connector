# What the agent can and can't do

The deliverable the assignment asks for: an honest account of the connector's
reach, written for whoever has to decide whether to trust it with a merchant.

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
- **Call Razorpay.** It finds the references; it does not look them up. Until
  a Razorpay tool is paired with it, `reconciliation` tells you where to look,
  not what the gateway says.
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
- **Refund reasons are free text** written by shop staff. A Razorpay refund id
  is only found there if someone pasted it.
- **Deleted and draft orders are invisible.** `trash` and `checkout-draft` are
  deliberately excluded from the allowed statuses.

### Limits of the deployment
- **One replica.** The rate budget is in-process. Several replicas sharing one
  store would each think they had the full budget; that needs a shared
  backend, which is sketched in ARCHITECTURE.md but not built.
- **One store per process.** There is no tenant registry. Multi-merchant
  hosting would need the token-to-tenant binding this connector does not yet
  have.
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
