# CLAUDE.md — Freshdesk connector for Agent Studio

Context for Claude Code working in this repo. Read this first; details live in `docs/`.

## What this is

A **read-only, payment-aware Freshdesk connector exposed as an MCP server** for Razorpay **Agent Studio** agents. It was built as the Razorpay Forward-Deployed Engineer take-home, **Option 3** ("build a private connector for a merchant tool"; Freshdesk chosen from Freshdesk / Zoho Inventory / WooCommerce / Unicommerce).

Option 3 asked for: an OAuth or API-key auth flow, list/get/search primitives, rate-limit handling, an MCP tool spec, and a short doc on what the agent can and can't do. All of these are done; the README table maps each requirement to its file.

The differentiator is that the connector is **built for Razorpay merchants**: every ticket carries `signals` (Razorpay `pay_/order_/rfnd_/sub_…` ids, UPI UTR/RRN, card ARN, ₹ amounts, merchant order id, an explainable intent, SLA state), and `support_pulse` ranks the backlog with reasons.

Repo: https://github.com/Rahulx911/freshdesk-connector (public). Version 0.3.0. CI is green.

## Layout

```
src/freshdesk_connector/
  auth.py          API-key creds, 0600 store, domain validation (SSRF: https only, no IPs/internal hosts)
  tenancy.py       tenant registry (hashed bearer tokens -> one merchant), RegistryWatcher hot-reload
  ratelimit.py     credit budgets: in-memory RateLimiter + RedisRateLimiter (atomic Lua, Redis TIME)
  client.py        httpx client: acquire budget -> GET -> settle; 429 Retry-After, 5xx backoff, fail-fast
  query.py         typed filters -> Freshdesk search language (model never writes query syntax)
  service.py       primitives: list/search/get tickets, conversations, contacts, companies,
                   customer_ticket_history, support_pulse, connector_status; status catalogue (TTL)
  normalize.py     LLM-shaped records, private-note policy, PII mask, signals + source_url
  insights.py      payment refs / intent / SLA (deterministic rules, no network)
  guardrails.py    prompt-injection flags, response size budget (binary search), event tracking
  observability.py JSON logs, audit line per tool call (PII-masked), Prometheus metrics
  mcp_server.py    11 tools + 2 prompts, _call wrapper, ServiceProvider (per-tenant), /healthz /readyz /metrics
  cli.py           auth login|status|logout, token create, serve, export-spec
mock_server/       FastAPI Freshdesk test double (fictional "Kettle & Leaf" data; real limits, headers, search grammar)
evals/             cases.json (19 scenarios) + run.py (--oracle no-LLM, --llm with ANTHROPIC_API_KEY)
scripts/           demo.py (end-to-end over MCP), loadtest.py, smoke_compose.sh
tests/             160 tests incl. Hypothesis fuzzing; Redis tests need `redis-server` (skipped if absent)
docs/              CAPABILITIES, ARCHITECTURE, SECURITY, RUNBOOK, FDE_PLAYBOOK, TESTING, mcp_tool_spec.json
```

## Commands

```bash
python -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"
pytest -q                                   # 160 tests (~20s)
pytest -q -m "not slow"                     # skip tests that spawn servers
python scripts/demo.py                      # expect "24/24 checks passed"
python -m evals.run --oracle                # expect 19/19
ruff check . && mypy && bandit -q -r src    # all must be clean (CI enforces)
pip-audit --strict -r requirements.lock --require-hashes
freshdesk-connector export-spec -o docs/mcp_tool_spec.json   # after ANY tool/description change
bash scripts/smoke_compose.sh               # Docker: 2 replicas + Redis + mock (needs Docker Hub access)
```

Run against a real Freshdesk (read-only): `FRESHDESK_DOMAIN=<sub> FRESHDESK_API_KEY=<key> python scripts/demo.py --live`. Never commit keys.

## Invariants: don't break these

1. **Read-only.** Only GET requests; every tool is `readOnlyHint=True`. Write tools would need human approval and their own scope; don't add them casually.
2. **The tenant comes from the bearer token** (`current_tenant()` → `get_access_token().client_id`), never from tool arguments.
3. **No secrets in the repo, logs or tool output.** Inline `api_key` in the registry is rejected. Tests grep logs for leaks.
4. **The model never writes Freshdesk query syntax.** Add typed params in `query.py`, with allow-listed characters.
5. **Signals stay deterministic and local** (`insights.py`). Each intent carries `intent_evidence`. Extend the false-positive corpus in `tests/test_signals.py` whenever you add a pattern.
6. **Errors are JSON** `{error, message, hint}` via `FreshdeskError.to_dict()`. Unexpected exceptions become `internal_error`.
7. **Freshdesk facts follow the official API docs** (decimal rate headers, `include` costs 2 credits, custom statuses ≥6 from `/ticket_fields`, inclusive `:>`/`:<` dates, search 30×10 pages, 512-char query). These are pinned in `tests/test_api_conformance.py`.

## Gotchas

- **The tool spec is checked by a test.** `test_export_spec_matches_committed_file` fails if `docs/mcp_tool_spec.json` is stale. Regenerate it.
- **The tool count is asserted** (11) in `test_cli.py`, `test_extended.py` and `test_mcp.py` (`EXPECTED`). Update all three when adding a tool.
- **Mock data is relative to `NOW`** (`mock_server/data.py`), so SLA states and the 30-day window stay realistic. Eval facts (`evals/cases.json`) depend on it; rerun `python -m evals.run --oracle` after changing mock data.
- Docstrings become tool descriptions; `build_server` runs them through `inspect.cleandoc` so Python 3.13 and older serve identical text.
- `pytest-asyncio` async fixtures that open MCP sessions break anyio cancel scopes; open sessions inside the test (see `tests/test_evals.py::open_session`).
- Coverage includes subprocesses (`[tool.coverage.run] patch = ["subprocess"]`).
- **Pushing:** history on GitHub was created through web uploads, so this local clone tracks GitHub's `main`. Push as **one commit** so CI runs once on a consistent tree. If you ever upload piecemeal, put `[skip ci]` on every commit except the last.

## Status / next steps

- Done: all Option 3 requirements; hosted multi-tenant mode; Redis shared budget; guardrails; observability; eval harness; Docker; CI on Python 3.10–3.13 (lint/types/security/tests/evals/container).
- Not yet verified against a **live Freshdesk account** (needs a trial). Run `demo.py --live`.
- LLM-mode evals need an `ANTHROPIC_API_KEY` repo secret; the CI step is skipped until it's set.
- Natural next features: a Razorpay Payments/Refunds tool pairing with `signals.payment_refs`; write tools behind approval; Freshdesk webhooks → Agent Studio triggers (see `docs/FDE_PLAYBOOK.md`).
