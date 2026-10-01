# Forward-deployed rollout playbook

The connector is the easy part. Whether it matters for a merchant depends on finding the real problem behind "connect our Freshdesk", then proving the impact with numbers. This is how I'd run it with a merchant.

## 1. Discovery (week 0, ~2 calls + a data pull)

Questions I'd ask:

- What share of tickets are "where is my order / refund / payment?" (WISMO)? *Usually the biggest bucket for D2C merchants, and the one an agent can answer end to end.*
- Which of those need data that **isn't in Freshdesk**, such as refund status, payment failure reasons or settlement dates? *For a Razorpay merchant this is often the real problem: agents spend their time copying payment status from the Razorpay dashboard into replies.*
- Who would use the agent: end customers on the website/WhatsApp, or the support team as a copilot? *This decides the private-note and PII settings.*
- What's the Freshdesk plan (rate limit), how many agents, and what other integrations share the quota?
- What must never happen? For example: promising refunds, quoting internal notes, or answering for the wrong customer.

The data pull: run `support_pulse` and 2–4 weeks of `search_tickets` / `list_tickets`, read-only, with this connector. `signals.intent` sizes each bucket on day one, and the share of payment-related tickets that already carry Razorpay ids tells you how much can be auto-resolved.

## 2. Baseline metrics (before the agent)

| Metric | Source |
|---|---|
| Ticket volume by intent | Ticket export + classification |
| First response time, resolution time | Freshdesk `stats` (exposed by `get_ticket`) |
| Agent handle time per WISMO ticket | Sampled, or from the time-entries API |
| Reopen rate, CSAT | Freshdesk reports |

## 3. Pilot (weeks 1–3)

- **Shadow mode first.** The agent drafts answers for the support team (an internal copilot with `private_notes: include`) and doesn't talk to customers. Track the acceptance rate of drafts and the edits made.
- **Expand the eval set from real tickets.** Turn 30–50 real (anonymised) conversations into `evals/cases.json` entries with expected tools and facts. Run them on every prompt or tool change (`--llm --repeats 3`) and gate releases on the pass rate.
- **Guardrail review.** Read every `content_flags` hit and every `rate_limited` / `internal_error` in the audit log each week with the merchant's support lead.

## 4. Go live (customer-facing)

- Switch the tenant to `private_notes: exclude` and consider `redact_pii: true` if the agent never needs to show contact details.
- Roll out by channel or by intent (WISMO first). Hand off to a human whenever the agent hits `rate_limited`, has no matching ticket, or sees a flagged injection.
- Success criteria agreed up front, for example: at least 40% of WISMO conversations resolved without a human, no rise in reopen rate, and zero private-note or wrong-customer incidents.

## 5. What I'd build next for a Razorpay merchant

1. **Close the loop with Razorpay data.** The connector already pulls `pay_` / `rfnd_` / `sub_` ids, UTRs and ARNs out of every ticket and thread (`signals.payment_refs`). Paired with Razorpay's Payments/Refunds tools in Agent Studio, "where's my refund?" becomes a fully grounded, automatic answer: refund status, ARN for the bank and expected credit date. This is the step that turns ticket lookup into resolution, and it's where I'd measure deflection first.
2. **Write tools behind approval.** `add_private_note` (the agent's summary for humans) first, then `reply_to_ticket` with a human approving each send in Agent Studio. Each is its own scope and audited.
3. **Events instead of polling.** A Freshdesk automation webhook on "ticket created" triggers the agent to draft a reply before a human opens the ticket.

## Hand-off checklist

- [ ] Tenant onboarded per `RUNBOOK.md`; `connector_status` verified with the merchant
- [ ] Integration agent restricted to the right groups
- [ ] Eval set from the merchant's real tickets passing at the agreed threshold
- [ ] Dashboards and alerts from `RUNBOOK.md` wired to the merchant's channel
- [ ] Baseline and 2-week post-launch metrics shared with the merchant
