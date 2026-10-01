# Freshdesk connector for Agent Studio

A production-grade, **read-only**, **payment-aware** connector that lets Agent Studio agents read a merchant's Freshdesk **tickets, conversations, contacts and companies** through **MCP**. It covers Razorpay's Forward-Deployed Engineer assignment, Option 3.

## Why it's different: built for Razorpay merchants, not just for Freshdesk

A large share of a D2C merchant's support tickets are payment questions: "refund not received", "charged twice", "autopay debited". The answer lives in **Razorpay**, not in the helpdesk. A plain Freshdesk reader leaves the agent to guess. This connector turns every ticket into something an agent can act on:

| | What the agent gets | Why it matters |
|---|---|---|
| **Payment references** | `signals.payment_refs`: Razorpay `pay_` / `order_` / `rfnd_` / `sub_` / `inv_`… ids, UPI UTR/RRN, card refund ARN, ₹ amounts and the merchant's order id, collected across the ticket **and its whole thread** | The agent can call Razorpay's Payments/Refunds APIs and answer "where is my refund?" from the source of truth, without copy-paste |
| **Explainable intent** | `signals.intent` (refund_status, double_charge, payment_failed, autopay_mandate, settlement, delivery…) **with the phrase that triggered it** | Routing and playbooks the merchant can audit; no customer text is sent to a third-party model |
| **SLA risk** | `signals.sla` from Freshdesk's own `due_by` / `fr_due_by`: overdue, due soon, first response missed | The agent knows when to apologise first and escalate |
| **One-call triage** | `support_pulse`: the active backlog by intent, priority and status, plus a ranked `needs_attention` list **with reasons** ("SLA overdue by 30h · high priority · payment issue with Razorpay refs") | The question a support lead asks every morning, answered in a single call (≈3 API credits) |
| **Citations** | `source_url` deep link on every ticket | Answers can be checked with one click |
| **Guided workflows** | MCP prompts `resolve_payment_ticket` and `daily_triage` | Agent Studio can offer ready-made, safe workflows: verify before promising, never invent a refund status |

Example `support_pulse` entry (mock data):
```json
{"id": 26, "subject": "Refund not received for cancelled order", "priority": "high", "intent": "refund_status",
 "score": 75, "why": ["SLA overdue by 30h", "high priority", "payment issue (refund_status) with Razorpay refs"],
 "source_url": "https://kettleandleaf.freshdesk.com/a/tickets/26"}
```

[![ci](https://github.com/Rahulx911/freshdesk-connector/actions/workflows/ci.yml/badge.svg)](https://github.com/Rahulx911/freshdesk-connector/actions/workflows/ci.yml)

```
Agent Studio agent ──MCP (HTTPS + bearer token)──▶ connector replicas ──HTTPS + API key──▶ merchant.freshdesk.com
                                                    │ token → tenant · credit budget (Redis, shared)
                                                    │ safe queries · guardrails · audit + metrics
```

## What's in it

| Assignment asks for | Where |
|---|---|
| OAuth / API-key auth flow | API key validated against `/agents/me` before saving (`auth login`); `0600` store or env or secret files. Hosted mode adds **bearer-token auth per agent, bound to one merchant** via MCP's OAuth resource-server hooks (`tenancy.py`). |
| list / get / search primitives | 11 tools: `list_tickets`, `search_tickets`, `get_ticket`, `list_ticket_conversations`, `find_contacts`, `get_contact`, `customer_ticket_history`, `find_companies`, `get_company`, `support_pulse`, `connector_status`, plus 2 MCP prompts |
| Rate-limit handling | Budget counted in **API credits** (Freshdesk charges `include`s extra), learned from the account's headers, 20% reserve for the merchant's other apps, **shared across replicas through an atomic Redis script**, `Retry-After`, bounded waits that fail fast with `retry_after_seconds` |
| MCP tool specification | `mcp_server.py`; exported JSON in [`docs/mcp_tool_spec.json`](docs/mcp_tool_spec.json) |
| What the agent can / can't do | [`docs/CAPABILITIES.md`](docs/CAPABILITIES.md) |
| Working test script | `scripts/demo.py` (end-to-end over MCP), 160 automated tests including property-based fuzzing, 19 agent eval scenarios, load test, compose smoke test. See [`docs/TESTING.md`](docs/TESTING.md) |

Production extras:
- **LLM guardrails:** customer-written text is flagged when it looks like prompt injection; private notes are withheld by default; responses have a size budget; there are no write tools.
- **Observability:** JSON audit log per tool call (PII-masked), Prometheus `/metrics`, `/healthz`, `/readyz`.
- **Agent eval harness:** 19 merchant scenarios (lookups, multi-hop, search, payments, triage, safety, errors) scored on tool choice, arguments, facts and safety. Oracle mode runs in CI; LLM mode runs with Claude when an API key is present.
- **Zero-downtime operations:** the tenant registry hot-reloads (revoke a token or onboard a merchant in seconds, no redeploy), Freshdesk key rotation is picked up without a restart, and one merchant's bad config degrades only that merchant, not the replica.
- **Supply chain and runtime:** hash-pinned lockfile, `pip-audit`, `bandit`, `ruff`, `mypy`. The Docker image runs non-root with a read-only filesystem, and compose runs 2 replicas plus Redis.
- Docs: [architecture](docs/ARCHITECTURE.md), [security model](docs/SECURITY.md), [runbook](docs/RUNBOOK.md), [FDE rollout playbook](docs/FDE_PLAYBOOK.md).

## Quick start (no Freshdesk account needed)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

pytest -q                       # 160 tests (Redis tests need redis-server; skipped otherwise)
python scripts/demo.py          # mock Freshdesk + MCP server, every tool, ends "24/24 checks passed"
python -m evals.run --oracle    # agent eval dataset against the connector, "19/19 passed"
```

## Use it

**Local, one merchant (stdio).** This is what an MCP client launches:

```bash
freshdesk-connector auth login --domain yourcompany     # key prompt hidden; verified before saving
freshdesk-connector serve                               # stdio
```

```json
{"mcpServers": {"freshdesk": {"command": "freshdesk-connector", "args": ["serve"]}}}
```

**Hosted, many merchants (streamable HTTP):**

```bash
freshdesk-connector token create --tenant acme --name agent-studio-acme-prod   # token shown once
freshdesk-connector serve --transport streamable-http --host 0.0.0.0 --port 8000 \
    --tenants deploy/tenants.json            # see deploy/tenants.example.json
# or: docker compose up   (2 replicas + Redis + mock; scripts/smoke_compose.sh runs it end to end)
```

The server refuses to listen on a public interface without a tenant registry. Onboarding, key rotation and alerts are in the [runbook](docs/RUNBOOK.md).

**Against a real Freshdesk (read-only):**
`FRESHDESK_DOMAIN=yourtrial FRESHDESK_API_KEY=... python scripts/demo.py --live`

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `FRESHDESK_DOMAIN`, `FRESHDESK_API_KEY` | – | stdio-mode credentials (override `auth login`) |
| `FRESHDESK_TENANTS_FILE` / `--tenants` | – | Tenant registry for hosted mode (no secrets inside) |
| `REDIS_URL` | – | Shared rate budget across replicas (required with >1 replica) |
| `FRESHDESK_RATE_RESERVE` | `0.2` | Share of the account quota left for the merchant's other apps |
| `FRESHDESK_MAX_WAIT_S` | `20` | Longest a tool call may wait before returning `rate_limited` |
| `FRESHDESK_PRIVATE_NOTES` | `exclude` | `include` only for internal copilots (per tenant in hosted mode) |
| `FRESHDESK_REDACT_PII` | `false` | Mask emails/phones in output (per tenant in hosted mode) |
| `FRESHDESK_MAX_BODY_CHARS` / `FRESHDESK_MAX_RESPONSE_CHARS` | `2000` / `60000` | Per-message truncation / per-response size budget |
| `METRICS_TOKEN` or `METRICS_TOKEN_FILE` | – | Bearer token for `/metrics` |
| `MCP_PUBLIC_URL`, `MCP_ALLOWED_HOSTS` | – | OAuth resource metadata URL; DNS-rebinding protection |
| `LOG_LEVEL`, `LOG_FORMAT`, `AUDIT_LOG` | `WARNING`, `text`, `on` | Use `json` in production; keep the audit log on |

## Assumptions and limitations

- Freshdesk API v2 with API-key Basic auth (`key:X`). Freshdesk has no merchant-facing OAuth grant for its REST API, so the OAuth piece is on the **agent → connector** side (bearer tokens per agent). Behaviour was checked against the official API reference; see [TESTING.md](docs/TESTING.md).
- Read-only. No attachment download, KB articles or satisfaction data yet.
- Freshdesk search has no full-text search, returns at most 300 results, skips archived tickets, and indexes with a short delay. Phone lookup only matches numbers in the format they were stored (common Indian formats are tried).
- The prompt-injection detector and intent rules are deterministic heuristics: explainable and tested for false positives, but not exhaustive. The real safety boundary is that there are no write tools and the tenant comes from the token.
- Payment references are extracted, not verified. The connector never claims a payment or refund status; that is for Razorpay's APIs.
- Everything is tested against a faithful mock and the docs. **No live Freshdesk account was used**; run `demo.py --live` on a trial account to close that gap.

## Layout

```
src/freshdesk_connector/   auth, tenancy, ratelimit, client, query, service, normalize,
                           insights (payment refs / intent / SLA), guardrails, observability, mcp_server, cli
mock_server/               Freshdesk test double (fictional data, real limits/headers/search syntax)
evals/                     agent eval dataset + harness (oracle and LLM modes)
scripts/                   demo.py, loadtest.py, smoke_compose.sh
tests/                     160 tests: unit, API-conformance, CLI, production (auth/Redis/guardrails), signals, fuzzing, evals
docs/                      CAPABILITIES, ARCHITECTURE, SECURITY, RUNBOOK, FDE_PLAYBOOK, TESTING, tool spec
Dockerfile, docker-compose.yml, deploy/, requirements.lock, .github/workflows/ci.yml
```
