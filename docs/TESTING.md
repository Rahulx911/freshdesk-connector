# Testing

```bash
pytest -q                     # 149 tests, ~5s
python scripts/demo.py        # 26/26 end-to-end over MCP against the mock
ruff check . && mypy && bandit -q -r src
```

Against a real store:

```bash
cd deploy/woo-local && docker compose up -d && ./bootstrap.sh
export WOO_STORE_URL=http://localhost:8080 WOO_CONSUMER_KEY=... WOO_CONSUMER_SECRET=...
python scripts/demo.py --live  # 15/15
```

## What each layer covers

| File | Covers |
|---|---|
| `test_auth.py` | URL validation and SSRF refusals, `0600` storage, env precedence, secret never in output |
| `test_api_conformance.py` | The WooCommerce behaviours a plausible implementation gets wrong. Each is a real bug avoided |
| `test_primitives.py` | The 11 tools against the mock store, including guest-checkout fallback |
| `test_signals.py` | Razorpay extraction, reconciliation states, and a **false-positive corpus**. Every new pattern must add a negative case |
| `test_guardrails.py` | Injection flagging, PII masking, response-size budget |
| `test_query.py` | Allow-lists, bounds, date normalisation |
| `test_rate_limits.py` | Budget maths, header learning, 429/503 handling, fail-fast, redirect refusal |
| `test_mcp.py` | Tool count, read-only annotations, structured errors, prompts |
| `test_fuzz.py` | Hypothesis properties: nothing raises, masking never leaks, output is always JSON |
| `test_production.py` | JSON logs, audit lines, credential never in a response |

## Rules

- **Every signal pattern needs a negative case.** A wrong payment reference
  means a customer is told the wrong thing about their money.
- **The tool spec is checked.** `test_export_spec_matches_the_committed_file`
  fails if `docs/mcp_tool_spec.json` is stale. Regenerate with
  `woocommerce-connector export-spec -o docs/mcp_tool_spec.json`.
- **The tool count is asserted** in `test_mcp.py` and `test_cli.py`.
