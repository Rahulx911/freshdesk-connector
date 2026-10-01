# CLAUDE.md — WooCommerce connector for Agent Studio

Context for Claude Code working in this repo. Read this first; details live in `docs/`.

## What this is

A **read-only, payment-aware WooCommerce connector exposed as an MCP server** for Razorpay **Agent Studio** agents. It was built as the Razorpay Forward-Deployed Engineer take-home, **Option 3** ("build a private connector for a merchant tool"; WooCommerce chosen from Freshdesk / Zoho Inventory / WooCommerce / Unicommerce).

The differentiator is **reconciliation**: a WooCommerce refund row only proves a shop manager clicked refund. Whether Razorpay moved the money is a different fact, recorded as a `rfnd_` id. `signals.reconciliation` surfaces that gap, and `store_pulse` ranks the backlog with reasons.

**The earlier Freshdesk version of this assignment is preserved on the `freshdesk-connector` branch.** Do not delete it.

## Layout

```
src/woocommerce_connector/
  auth.py          consumer key/secret, 0600 store, URL validation (https only, no IPs/internal hosts)
  oauth.py         OAuth 1.0a one-legged signing, mirroring WC_REST_Authentication
  ratelimit.py     client-side sliding-window request budget with a 20% reserve
  client.py        httpx: budget -> GET -> settle; 429/503 Retry-After, 5xx backoff, redirect refusal
  query.py         typed filters -> wc/v3 params (the model never writes query syntax)
  service.py       the 11 primitives
  normalize.py     LLM-shaped records, PII masking, HPOS-aware source_url
  insights.py      Razorpay refs / reconciliation / intent (deterministic, local, no network)
  guardrails.py    prompt-injection flags, response size budget (binary search)
  observability.py JSON logs, audit line per tool call (PII-masked), Prometheus metrics
  mcp_server.py    11 tools + 2 prompts
  cli.py           auth login|status|logout, serve, export-spec
deploy/woo-local/  real WordPress + WooCommerce in Docker, seeded, mints a Read-scoped key
mock_server/       FastAPI WooCommerce test double (fictional "Kettle & Leaf" data)
tests/             149 tests incl. Hypothesis fuzzing and API conformance
docs/              CAPABILITIES, ARCHITECTURE, SECURITY, RUNBOOK, TESTING, mcp_tool_spec.json
```

## Commands

```bash
python -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"
pytest -q                                   # 149 tests (~5s)
python scripts/demo.py                      # expect "26/26 checks passed"
ruff check . && mypy && bandit -q -r src    # all must be clean (CI enforces)
woocommerce-connector export-spec -o docs/mcp_tool_spec.json   # after ANY tool/description change

# real store, no accounts needed:
cd deploy/woo-local && docker compose up -d && ./bootstrap.sh
WOO_STORE_URL=http://localhost:8080 WOO_CONSUMER_KEY=ck_... WOO_CONSUMER_SECRET=cs_... \
  python scripts/demo.py --live             # expect "15/15 checks passed"
python scripts/assert_read_only.py          # proves the key cannot write
```

## Invariants: don't break these

1. **Read-only.** Only GET; every tool is `readOnlyHint=True`. The WooCommerce key is Read-scoped, so the store refuses writes independently of this code. Keep it that way.
2. **The model never writes query syntax.** Add typed params in `query.py` with allow-listed values.
3. **Signals stay deterministic and local** (`insights.py`). Every intent carries `intent_evidence`. **Extend the false-positive corpus in `tests/test_signals.py` whenever you add a pattern** — a wrong payment reference means a customer is told the wrong thing about their money.
4. **No secrets in the repo, logs or tool output.** Tests assert the consumer secret cannot appear in any response.
5. **Errors are JSON** `{error, message, hint}` via `WooError.to_dict()`. Unexpected exceptions become `internal_error`.
6. **WooCommerce facts follow the real API**, pinned in `tests/test_api_conformance.py`: Basic auth only over HTTPS, `X-WP-Total` paging headers, `per_page` cap 100, guest `customer_id = 0`, `role=all` for customers, HPOS admin links, naive ISO dates.

## Gotchas

- **Basic auth silently fails over plain HTTP.** WooCommerce only tries it when `is_ssl()`; otherwise it falls through to OAuth and an unsigned request authenticates as nobody, surfacing as `cannot_view`. That is why `oauth.py` exists.
- **The tool spec is checked by a test.** Regenerate `docs/mcp_tool_spec.json` after any tool or docstring change.
- **The tool count is asserted** (11) in `test_mcp.py` and `test_cli.py`.
- **WooCommerce needs WordPress 7+.** The local stack pins `wordpress:php8.3-apache`, not a 6.x tag.
- Docstrings become tool descriptions; `inspect.cleandoc` keeps 3.13 and older identical.

## Status / next steps

- Done: all Option 3 requirements; **both** auth flows; verified end to end against a real WordPress 7.1 + WooCommerce 11.1 store.
- Not built: shared rate budget across replicas, multi-tenant registry, webhooks, and the Razorpay Payments/Refunds tool that would turn `reconciliation` from advisory into actionable.
