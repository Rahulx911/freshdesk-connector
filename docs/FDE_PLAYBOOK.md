# Forward-deployed rollout playbook

The connector is the easy part. Whether it matters for a merchant depends on finding the real problem behind "connect our store", then proving the impact with numbers. This is how I would run it.

## 1. Discovery (week 0, about two calls and a data pull)

Questions I would ask:

- **What share of your support load is "where is my order / refund / payment?"** *Usually the biggest bucket for a D2C merchant, and the one an agent can answer end to end.*
- **When a customer asks where their refund is, where does your team look?** *If the answer is "the Razorpay dashboard, then the WooCommerce order", that is the problem this connector exists for. A refund row in WooCommerce is not evidence the money moved.*
- **Has a customer ever been told a refund was processed when it had not been?** *Almost every merchant has a story. WooCommerce itself warns about this on the order: "Order status set to refunded. To return funds to the customer you will need to issue a refund through your payment gateway." The order still reads `refunded` with a net payment of zero, so the warning is easy to miss. That note is the fastest way to make `signals.reconciliation` concrete rather than abstract.*
- **Who uses the agent: customers on the site or WhatsApp, or the shop team as a copilot?** *This decides the PII masking setting and how much order detail is safe to surface.*
- **What is the hosting, and what else hits the REST API?** *Shared hosting with a plugin-heavy store is where an impatient agent causes an outage. It sets the request budget.*
- **What must never happen?** *Usually: promising a refund, quoting the wrong customer's order, or exposing another buyer's address.*

**The data pull.** Run `store_pulse` over 30 days plus a few `search_orders` windows, read-only, with a Read-scoped key. On day one that gives you the order mix by payment state and intent, and two numbers that usually land hard: how many refunds carry no Razorpay refund id, and how many orders carry more than one payment id.

## 2. Baseline metrics (before the agent)

| Metric | Source |
|---|---|
| Support contacts by intent | `store_pulse` `by_intent` over 30 days |
| Refunds not confirmed at the gateway | `store_pulse` `refunds_not_confirmed_at_gateway` |
| Possible double charges | Orders with `reconciliation.status = multiple_payments` |
| Unpaid orders aging past 3 days | `search_orders` with `status=["pending","on-hold"]` |
| Handle time per payment question | Sampled with the support lead |

The first two are the ones to quote back. They are money already at risk, measured from the merchant's own data, before anything is built.

## 3. Pilot (weeks 1 to 3)

- **Shadow mode first.** The agent drafts answers for the shop team and does not talk to customers. Track draft acceptance and what gets edited.
- **Grow the eval set from real orders.** Turn 30 to 50 real, anonymised questions into `evals/cases.json` entries with expected tools and facts. Run `--oracle` on every change and `--llm --repeats 3` before a release. Gate on the pass rate.
- **Watch the safety cases specifically.** The `must_not_claim` checks exist because the expensive failure is not a wrong order number, it is the agent saying a refund has been sent when the connector reported it unconfirmed.
- **Weekly guardrail review** with the support lead: every `untrusted_text_flags` hit and every `rate_limited` or `internal_error` in the audit log.

## 4. Go live

- Keep PII masking on unless the agent genuinely needs contact details.
- Roll out by intent. Order status first, because it is high volume and low risk. Refund questions only once the Razorpay pairing in step 5 is in place.
- Hand off to a human whenever the connector returns `rate_limited`, finds no matching order, or flags injected text.
- Agree success criteria up front. For example: at least 40% of order-status conversations resolved without a human, no rise in repeat contacts, and zero wrong-customer incidents.

## 5. What I would build next for a Razorpay merchant

1. **Close the loop with Razorpay.** The connector finds the references; it does not resolve them. Paired with Razorpay's Payments and Refunds tools in Agent Studio, `reconciliation` stops being advisory. "Where is my refund?" becomes a grounded answer with the gateway's own status and expected credit date, and the unconfirmed-refund list becomes a worklist that closes itself. This is where I would measure deflection first.
2. **Write tools behind human approval.** An internal order note first, then a customer reply with a human approving each send. Each gets its own scope and its own audit trail. Note that this also needs a Read-Write key, which deliberately changes the security posture, so it is a conversation with the merchant rather than a flag.
3. **Events instead of polling.** A WooCommerce webhook on order status change, HMAC verified, triggering the agent to draft before a human opens the order.
4. **A shared request budget.** The moment there is more than one replica against one store, the in-process budget is wrong. Same interface, shared backend.

## Hand-off checklist

- [ ] Read-scoped key issued, `connector_status` verified with the merchant
- [ ] `assert_read_only.py` run against their store, output shared with their security reviewer
- [ ] Eval set built from the merchant's real orders, passing at the agreed threshold
- [ ] Baseline numbers from step 2 shared, with the at-risk refund count called out
- [ ] Audit log and metrics wired into the merchant's own monitoring
- [ ] Runbook walked through with whoever is on call
