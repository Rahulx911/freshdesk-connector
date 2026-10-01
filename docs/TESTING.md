# Testing report

| Check | Result |
|---|---|
| Automated tests | **160 passed** on Python 3.10, 3.11 and 3.13 (CI also runs 3.12), including property-based fuzzing (Hypothesis, 400 generated cases per property) |
| Line coverage | **93%**, including hosted-mode code that runs in subprocesses |
| End-to-end MCP demo | **24/24** checks |
| Agent evals (oracle mode) | **19/19** scenarios (incl. payments and triage) |
| Load test, 2 replicas | 2,533 calls in 30 s, **0 errors**, p50 204 ms / p95 399 ms / p99 508 ms |
| Quota-protection burst | 100 calls against a 50-credit/min account: exactly **40 credits** used (80%), **0 upstream 429s**, the rest failed fast (p95 292 ms) |
| Compose smoke test | Image build, 2 hardened replicas + Redis + mock, 401 without a token, health/readiness, budget under burst, metrics, no secrets in logs: **passed** |
| Lint / types / security | `ruff`, `mypy`, `bandit` clean; `pip-audit` on the hash-pinned lockfile: no known vulnerabilities |

```bash
pip install -e ".[dev]"
pytest -q --cov                          # all tests (+ coverage, subprocesses included)
pytest -q -m "not slow"                  # skip tests that start servers
python scripts/demo.py                   # end-to-end over MCP
python -m evals.run --oracle             # eval dataset vs connector, no LLM
ANTHROPIC_API_KEY=... python -m evals.run --llm --repeats 3   # Claude drives the tools
scripts/smoke_compose.sh                 # production shape in Docker
python scripts/loadtest.py --url http://127.0.0.1:8000 --token $TOKEN --concurrency 20 --duration 30
```

## Test suites

| File | What it proves |
|---|---|
| `test_auth.py`, `test_cli.py` | Domain validation (including SSRF cases: IP literals, `.internal`/`.svc` hosts, paths); bad keys never stored; `0600` store; login → status → logout against live HTTP; spec export matches the committed spec |
| `test_primitives.py` | Every list/get/search primitive, pagination, validation, HTML → text, truncation, PII masking, private-note policy |
| `test_api_conformance.py` | Behaviour pinned to the official Freshdesk API docs (see below) |
| `test_rate_limits.py`, `test_extended.py` | 429/`Retry-After`, proactive throttling, fail-fast, 5xx retries, 20 concurrent calls with zero 429s, real wall-clock limits, streamable-HTTP transport, injection inputs never reach Freshdesk, key never logged |
| `test_production.py` | **Hosted mode:** 401 without or with a wrong token; the token selects the merchant and per-merchant policy; health, readiness and protected metrics; JSON audit log with masked PII and no secrets; registry validation; refuses public HTTP without auth. **Redis:** two replicas share one budget (8 of 8 granted, zero 429s); charges corrected from headers; 100 racing acquires grant exactly the budget. **Guardrails:** 8 attack strings flagged and 10 normal support messages not flagged; flags on ticket and thread; response size budget; stale quota observations expire |
| `test_signals.py` | Razorpay ref / UTR / ARN / amount extraction with a false-positive corpus (phones, pincodes, GSTINs, wrong-length ids); intent rules; SLA states; refs collected across the thread; `support_pulse` ranking, reasons and credit cost; status-cache TTL, single-flight and transient-error handling; HTML noise stripping; strict size budget; registry hot-reload and revocation; broken edits keep the last good registry; key rotation without restart; per-tenant readiness |
| `test_fuzz.py` | Properties: any accepted search tag compiles to exactly one tag clause (no smuggled operators); HTML conversion leaves no tags; every email is masked; detectors never crash; size budget always holds |
| `test_evals.py` | Oracle run passes; the agent loop and scorer driven by a scripted model (correct run passes; wrong tool and leaked private note are caught; tool errors reach the model) |

## Checked against the real Freshdesk API docs

The mock only proves the code matches my reading of Freshdesk, so I checked the connector against the official API v2 reference. That found four problems, all fixed and pinned by `test_api_conformance.py`:

1. **Decimal rate-limit headers** (`X-Ratelimit-Total: 700.0`) were parsed as integers. The connector never learned the real limit.
2. **`include` costs 2 extra credits.** The budget counted requests, not credits. It now predicts the cost and corrects it from `X-RateLimit-Used-CurrentRequest`.
3. **Statuses 6+ are configured per account.** They're now loaded from `/ticket_fields`, with a fallback that labels unknown ones `status_<id>` rather than guessing.
4. **Search `:>` / `:<` include the boundary date**, and `null` matches empty fields. Docs and tests now say inclusive, and there's a new `unassigned` filter.

Also pinned: the 30-day default list window, 100 per page, search at 30 per page for 10 pages, the 512-character query cap, exact-match phone filters, the shape of the company autocomplete response, and Basic auth `key:X`.

## Bugs found by testing

- **The quota back-off never expired.** A low "remaining" reading from Freshdesk kept forcing waits forever once traffic stopped. Found when the client started rechecking its budget after sleeping; fixed by expiring observations after one window (`test_stale_remaining_does_not_block_forever`).
- **The published tool spec was stale** after behaviour changes. The CLI test now fails whenever `docs/mcp_tool_spec.json` drifts.
- **Python 3.13 changed tool descriptions.** 3.13 strips docstring indentation, so the model saw different text depending on the Python version. Descriptions are now normalised.
- **The model would have had to count thread messages itself.** The eval oracle showed "how many messages?" wasn't directly answerable; `get_ticket` now returns `conversations_returned`.
- **The compose stack would have refused to start.** The mock host is plain HTTP and the connector correctly rejected it; there's now an explicit, test-only allowlist (`FRESHDESK_INSECURE_HTTP_HOSTS`).

## Second audit (line-by-line review + fuzzing)

| Finding | Impact | Fix | Test |
|---|---|---|---|
| A transient error (rate limit, Freshdesk blip) while loading custom statuses pinned the defaults **forever** | "waiting_on_customer" tickets mislabelled until restart | Transient errors aren't cached; 1-hour TTL; single-flight lock | `test_transient_error_does_not_pin_default_statuses`, `test_status_catalogue_ttl_and_single_flight` |
| **Email masking leaked part of addresses** with characters like `'` or `!` before the `@` (`o'b***@…`) | PII in audit logs / redacted output | Full RFC 5322 local-part character set | Found by `test_fuzz.py`; regression in `test_mask_pii_regressions` |
| One merchant's missing key made `/readyz` fail the whole replica | One bad config takes every merchant offline | Readiness degraded per tenant | `test_key_rotation_rebuilds_client_and_readiness_is_per_tenant` |
| Rotated Freshdesk keys weren't picked up until restart | `auth_failed` after a routine rotation | Credentials re-read per call; client rebuilt when the key changes | same |
| Token revocation needed a redeploy | Slow incident response | Registry hot-reload (~5 s), last-good on broken edits | `test_registry_hot_reload_*`, `test_broken_registry_edit_keeps_last_good` |
| `<style>`, `<script>` and comment contents leaked into ticket text | Junk and possible hidden instructions in the model context | Stripped before conversion | `test_html_noise_removed` |
| Size budget re-serialised the response per dropped item (O(n²)) and could overshoot by the marker size | Latency on huge threads; budget off by ~60 chars | Binary search with a margin for markers | `test_size_budget_is_strict_and_fast`, fuzz property |

## Not covered, and why

- **A live Freshdesk account.** Creating one needs the merchant's sign-up. `scripts/demo.py --live` is read-only and ready for a trial account.
- **LLM-mode evals with a real model.** No API key in this environment. The harness and scorer are tested with a scripted model, and the CI job runs `--llm --repeats 3` automatically once an `ANTHROPIC_API_KEY` secret is added.
- **Container base image locally.** Public registries are blocked in the build sandbox, so the local image test used a stand-in base. The CI `container` job builds from `python:3.12-slim` and runs the same smoke test.
