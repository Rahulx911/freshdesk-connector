# Freshdesk connector for Agent Studio

A production-grade, **read-only** connector that lets Agent Studio agents read a merchant's Freshdesk **tickets, conversations, contacts and companies** through **MCP**. It covers Razorpay's Forward-Deployed Engineer assignment, Option 3.

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
| list / get / search primitives | 10 tools: `list_tickets`, `search_tickets`, `get_ticket`, `list_ticket_conversations`, `find_contacts`, `get_contact`, `customer_ticket_history`, `find_companies`, `get_company`, `connector_status` |
| Rate-limit handling | Budget counted in **API credits** (Freshdesk charges `include`s extra), learned from the account's headers, 20% reserve for the merchant's other apps, **shared across replicas through an atomic Redis script**, `Retry-After`, bounded waits that fail fast with `retry_after_seconds` |
| MCP tool specification | `mcp_server.py`; exported JSON in [`docs/mcp_tool_spec.json`](docs/mcp_tool_spec.json) |
| What the agent can / can't do | [`docs/CAPABILITIES.md`](docs/CAPABILITIES.md) |
| Working test script | `scripts/demo.py` (end-to-end over MCP), 118 automated tests, agent evals, load test, compose smoke test. See [`docs/TESTING.md`](docs/TESTING.md) |

Production extras:
- **LLM guardrails:** customer-written text is flagged when it looks like prompt injection; private notes are withheld by default; responses have a size budget; there are no write tools.
- **Observability:** JSON audit log per tool call (PII-masked), Prometheus `/metrics`, `/healthz`, `/readyz`.
- **Agent eval harness:** 15 merchant scenarios scored on tool choice, arguments, facts and safety. Oracle mode runs in CI; LLM mode runs with Claude when an API key is present.
- **Supply chain and runtime:** hash-pinned lockfile, `pip-audit`, `bandit`, `ruff`, `mypy`. The Docker image runs non-root with a read-only filesystem, and compose runs 2 replicas plus Redis.
- Docs: [architecture](docs/ARCHITECTURE.md), [security model](docs/SECURITY.md), [runbook](docs/RUNBOOK.md), [FDE rollout playbook](docs/FDE_PLAYBOOK.md).

## Quick start (no Freshdesk account needed)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

pytest -q                       # 118 tests (Redis tests need redis-server; skipped otherwise)
python scripts/demo.py          # mock Freshdesk + MCP server, every tool, ends "21/21 checks passed"
python -m evals.run --oracle    # agent eval dataset against the connector, "15/15 passed"
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
- The prompt-injection detector is heuristic; the real safety boundary is that there are no write tools and the tenant comes from the token.
- Everything is tested against a faithful mock and the docs. **No live Freshdesk account was used**; run `demo.py --live` on a trial account to close that gap.

## Layout

```
src/freshdesk_connector/   auth, tenancy, ratelimit, client, query, service, normalize,
                           guardrails, observability, mcp_server, cli
mock_server/               Freshdesk test double (fictional data, real limits/headers/search syntax)
evals/                     agent eval dataset + harness (oracle and LLM modes)
scripts/                   demo.py, loadtest.py, smoke_compose.sh
tests/                     118 tests: unit, API-conformance, CLI, production (auth/Redis/guardrails), evals
docs/                      CAPABILITIES, ARCHITECTURE, SECURITY, RUNBOOK, FDE_PLAYBOOK, TESTING, tool spec
Dockerfile, docker-compose.yml, deploy/, requirements.lock, .github/workflows/ci.yml
```
