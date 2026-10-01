# Freshdesk connector for Agent Studio

A private, **read-only** connector that lets an Agent Studio agent read a merchant's Freshdesk **tickets, ticket conversations, contacts and companies**. It's exposed as an **MCP server** with 10 tools.

Built for Razorpay's Forward-Deployed Engineer assignment (Option 3).

```
Agent Studio agent ──MCP (stdio / streamable HTTP)──▶ freshdesk-connector ──HTTPS + API key──▶ <merchant>.freshdesk.com
                                                        │ auth · rate limiter · retries
                                                        │ safe query builder · normalizer · PII mask
```

| Requirement | Where |
|---|---|
| Auth flow (API key) | `auth.py`, `freshdesk-connector auth login/status/logout` |
| list / get / search primitives | `service.py`: `list_tickets`, `search_tickets`, `get_ticket`, `list_ticket_conversations`, `find_contacts`, `get_contact`, `customer_ticket_history`, `find_companies`, `get_company`, `connector_status` |
| Rate-limit handling | `client.py` (`RateLimiter` + 429/`Retry-After` + bounded wait) |
| MCP tool specification | `mcp_server.py`; exported JSON in [`docs/mcp_tool_spec.json`](docs/mcp_tool_spec.json) |
| What the agent can / can't do | [`docs/CAPABILITIES.md`](docs/CAPABILITIES.md) |
| Working test script | `scripts/demo.py` (end-to-end over MCP) + `tests/` (73 pytest cases). Results in [`docs/TESTING.md`](docs/TESTING.md) |

## Quick start (no Freshdesk account needed)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

pytest -q                      # 73 tests: auth, primitives, search, rate limits, API-doc conformance, MCP (stdio + HTTP)
python scripts/demo.py         # spins up a mock Freshdesk + the MCP server, calls every tool
```

`scripts/demo.py` starts the bundled mock Freshdesk (`mock_server/`, fictional data for a made-up tea brand). It then launches the connector **as an MCP stdio server** and calls every tool through a real MCP client session, the same way an agent runtime does. It covers:
- happy paths for every tool, including pagination;
- failure cases: unknown id, an injection attempt in a search tag, an invalid enum, ambiguous arguments, and a wrong API key;
- rate limiting against a 10 req/min account: the connector throttles itself at 8 and then returns a structured `rate_limited` error.

Expected output ends with `21/21 checks passed`.

## Using a real Freshdesk account

1. Get the API key in Freshdesk: profile picture → **Profile settings** → **View API key**. Use a dedicated integration agent with read access to all tickets if you can.
2. Authenticate. The key is checked against `GET /api/v2/agents/me` before it is saved:
   ```bash
   freshdesk-connector auth login --domain yourcompany      # key prompt is hidden
   freshdesk-connector auth status
   ```
   Credentials are stored in `~/.config/freshdesk-connector/credentials.json` with mode `0600`. Alternatively, set `FRESHDESK_DOMAIN` and `FRESHDESK_API_KEY`; environment variables take precedence, which suits containers and secret managers.
3. Run the read-only live demo:
   ```bash
   FRESHDESK_DOMAIN=yourcompany FRESHDESK_API_KEY=... python scripts/demo.py --live
   ```
4. Register with an agent runtime. Any MCP client works; example config:
   ```json
   {
     "mcpServers": {
       "freshdesk": {
         "command": "freshdesk-connector",
         "args": ["serve"],
         "env": { "FRESHDESK_DOMAIN": "yourcompany", "FRESHDESK_API_KEY": "${secret:freshdesk}" }
       }
     }
   }
   ```
   For a hosted deployment use `freshdesk-connector serve --transport streamable-http --port 8000`.

### Configuration

| Env var | Default | Purpose |
|---|---|---|
| `FRESHDESK_DOMAIN` / `FRESHDESK_API_KEY` | – | Credentials (override the stored ones) |
| `FRESHDESK_RATE_RESERVE` | `0.2` | Share of the account's per-minute quota left for the merchant's other apps |
| `FRESHDESK_MAX_WAIT_S` | `20` | Longest a single tool call may wait on rate limits or retries before returning `rate_limited` |
| `FRESHDESK_REDACT_PII` | `false` | Mask emails and phone numbers in tool output |
| `FRESHDESK_MAX_BODY_CHARS` | `2000` | Truncation limit for descriptions and messages |
| `LOG_LEVEL` | `WARNING` | Logs go to stderr; the API key is never logged |

## Design decisions

- **Read-only first.** A support agent that can only read is low-risk and still answers most of the questions merchants actually ask ("where's my order/refund?", "what's open and urgent?"). Write actions come later, behind approval.
- **No free-form query strings from the model.** Freshdesk search uses a mini query language. Tools take typed arguments (enums for status and priority, ISO dates, allow-listed characters for tags), and `query.py` builds the query string. This blocks injection (`tag:'x' OR status:5`) and removes syntax errors.
- **Shaped for an LLM.** Enum codes become words, HTML becomes text, long bodies are truncated, and empty fields are dropped. Pagination is explicit, and there are warnings when Freshdesk's limits mean the agent sees only part of the data.
- **Errors the model can act on.** Every error carries a code, a message and a `hint` (see CAPABILITIES.md).
- **Polite with a shared quota.** The connector keeps a 20% reserve, so it can't starve the merchant's other Freshdesk apps. The budget is counted in API *credits*, because Freshdesk charges extra for `include`.
- **Statuses come from the account.** Custom statuses ("Waiting on Customer", …) are read from `/ticket_fields`, so they're never hard-coded.
- **One composite tool.** `customer_ticket_history(email)` covers the most common opening question in one call instead of two.

## Assumptions

- Freshdesk API v2 with API-key Basic auth (`key:X`). Freshdesk has no OAuth grant for its own REST API, so the "auth flow" here is: validate the key, store it securely, revoke it with `logout`. On the Freshworks side, the merchant revokes it by resetting the key.
- One merchant per server process. Multi-tenant hosting is covered under "Long-term" below.
- Data in `mock_server/` is entirely fictional. No real customer data, keys or credentials are included anywhere in this repo.

## Limitations

- Read-only; no attachments, KB, canned responses or satisfaction data (see CAPABILITIES.md).
- No full-text search, because Freshdesk's API doesn't provide it. Search also skips archived tickets, and new changes take a few minutes to become searchable.
- Phone lookup only matches the number as it was stored. The connector tries common formats (raw, digits, last 10 digits, +91 variants), but an unusual stored format can still miss.
- Search returns at most 300 results per query (Freshdesk limit).
- Rate-limit state is per process. Several replicas sharing one account would each assume they have the full budget.
- The mock server implements only the subset of Freshdesk behaviour this connector uses. It is a test double, not a full emulator.

## Long-term

1. **Multi-tenant hosting inside Agent Studio.** Run as one streamable-HTTP service. Put per-merchant keys in a secrets manager (KMS-encrypted), selected by tenant id on each request. Keep the rate-limit budget in Redis, keyed by Freshdesk domain, so replicas share it.
2. **Writes with a human in the loop.** Add `add_private_note`, `reply_to_ticket` and `update_ticket_status` as separate tools marked `destructiveHint`. Draft first, then require the merchant agent's approval in Agent Studio before the call is made. Log every write with the agent run id.
3. **Events instead of polling.** Freshdesk automations or webhooks trigger Agent Studio runs on ticket create or update.
4. **Freshworks Marketplace app with OAuth.** For Freshworks-native OAuth and per-merchant install, package this as a Marketplace app.
5. **Evals.** Build a fixed set of merchant questions with expected tool calls and answers, run against the mock in CI, to catch prompt and tool-description regressions.

## Layout

```
src/freshdesk_connector/
  auth.py         API-key validation, 0600 credential store, env override
  client.py       async HTTP client, RateLimiter, retries, error mapping
  query.py        safe Freshdesk search-query builder
  normalize.py    enum mapping, HTML→text, truncation, PII masking
  service.py      list/get/search primitives
  mcp_server.py   MCP tools + server instructions
  cli.py          auth login|status|logout, serve, export-spec
mock_server/      FastAPI Freshdesk test double + fictional data
scripts/demo.py   end-to-end MCP demo (mock or --live)
tests/            pytest suite
docs/             CAPABILITIES.md, TESTING.md, mcp_tool_spec.json
.github/workflows CI: tests + demo on Python 3.10–3.13
```
