# Testing report

**Result:** 73 automated tests and 21 end-to-end MCP checks, all passing on Python 3.10, 3.11 and 3.13. Line coverage is 91%. GitHub Actions reruns everything on 3.10–3.13 on every push (`.github/workflows/ci.yml`).

```bash
pip install -e ".[dev]" pytest-cov
pytest -q --cov=freshdesk_connector        # 73 tests, ~6s
python scripts/demo.py                     # end-to-end over MCP, ends with "21/21 checks passed"
pytest -q -m "not slow"                    # skip the tests that start real servers
```

## What is tested

| Layer | File | What it proves |
|---|---|---|
| Auth | `test_auth.py`, `test_cli.py` | Domain normalisation and rejection of non-local plain HTTP. Bad keys are refused and never stored. The credential file is mode `0600`. The full login → status → logout flow runs against a live HTTP server. The key never appears in `repr`, status output or logs. |
| Primitives | `test_primitives.py` | list/get/search for tickets, conversations, contacts and companies. Pagination (`has_more` / `next_page`, no overlap between pages). Input validation. HTML → text. Truncation of long threads. PII masking. |
| Search safety | `test_primitives.py`, `test_extended.py` | The typed filters build Freshdesk's query string. Injection attempts (`x' OR status:5`, quotes, parentheses, HTML) are rejected **before any request reaches Freshdesk**. |
| Rate limits | `test_rate_limits.py`, `test_api_conformance.py`, `test_extended.py` | The connector learns the account's limit from headers and keeps a 20% reserve. It honours `Retry-After` on 429s, throttles itself before the server ever sends a 429, and fails fast with a structured `rate_limited` error when the wait would exceed the time budget. It retries on 5xx and gives up after the maximum number of retries. 20 concurrent calls share one budget with zero 429s. It also runs against a real server in real time. |
| MCP surface | `test_mcp.py`, `test_extended.py`, `test_cli.py` | 10 tools, all `readOnlyHint`. Errors come back as machine-readable JSON with a hint. Tools work over **stdio and streamable HTTP**. The committed `docs/mcp_tool_spec.json` matches what the server actually serves. |
| End to end | `scripts/demo.py` | Starts a mock Freshdesk, then runs the connector as an MCP stdio server and calls every tool through a real MCP client session, covering normal use, errors and rate limiting. |

## Checked against the real Freshdesk API docs

The mock only proves the code matches my own understanding of Freshdesk. So on 2026-10-01 I compared the connector with the official API v2 reference (developers.freshdesk.com/api). That found **four real problems**, now fixed and each covered by a test in `test_api_conformance.py`:

| # | What the docs say | Problem before | Fix |
|---|---|---|---|
| 1 | Rate-limit headers are decimals: `X-Ratelimit-Total: 700.0` | **Bug:** the headers were parsed as integers, so the connector never learned the real limit and stayed at the conservative default. | Parse as numbers with decimals; test uses the docs' exact example. |
| 2 | "Each include will consume an additional 2 credits"; including conversations costs 2 in total | The throttle counted every request as 1 credit, but `get_ticket` actually costs 5–6, so it could overspend the budget. | The budget now counts credits, predicts the cost from `include`, and corrects it from `X-RateLimit-Used-CurrentRequest`. |
| 3 | Statuses 2–5 are standard; others such as "Waiting on Customer" (6) are configured per account | 6 and 7 were hard-coded as if they were standard, which would mislabel tickets on other accounts. | Statuses are read from the account's `/api/v2/ticket_fields`. If that fails, it falls back to 2–5 and shows unknown ones as `status_<id>` rather than guessing. |
| 4 | Search `:>` / `:<` are "greater/less than **or equal to**"; `null` matches empty fields | Date bounds were documented as exclusive. There was no way to find unassigned tickets. | Date bounds are now documented as inclusive (the mock matches), and there's a new `unassigned` filter (`agent_id:null`). |

Other documented behaviour the tests also pin down: the 30-day default window for listing tickets, at most 100 per page, search returning 30 per page for at most 10 pages, the 512-character query limit, contact phone/mobile filters matching the exact stored value (the connector tries common Indian number formats), `{"companies": [...]}` as the response shape of company autocomplete, and Basic auth in the form `key:X`.

## Not tested, and why

- **A real Freshdesk account.** No live account was available. Creating one needs the merchant's sign-up, so it wasn't done here. Run `FRESHDESK_DOMAIN=... FRESHDESK_API_KEY=... python scripts/demo.py --live` against a free trial account; it is read-only. Steps that rely on mock-only data are skipped in `--live` mode.
- **An LLM driving the tools.** The tests exercise the MCP contract directly. Testing a model's tool choices needs an eval set of merchant questions with expected tool calls (README → Long-term).
- **Load beyond one process.** Rate-limit state is per process, so a multi-replica deployment needs a shared budget (README → Long-term).
