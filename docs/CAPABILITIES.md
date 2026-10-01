# What the agent can and cannot do

This is the one-page brief for the merchant and for whoever writes the Agent Studio agent's prompt.

## The short version

The agent can **read** the merchant's Freshdesk helpdesk: tickets, ticket threads, customers (contacts) and companies. It can find a customer by email, phone or name, see their ticket history, filter tickets by status, priority, tag and date, and read a full conversation thread.

It **cannot change anything**. It can't reply to customers, update tickets, add notes, assign tickets, or create or delete records. The connector only issues `GET` requests, and every MCP tool is marked `readOnlyHint: true`.

## Can do

| Agent question | Tool(s) |
|---|---|
| "What's the status of my complaint?" (customer gives email) | `customer_ticket_history` → `get_ticket` |
| "Show urgent open tickets about refunds this week" | `search_tickets(status=[open], priority=[urgent], tags=[refund], created_after=…)` |
| "What has changed since this morning?" | `list_tickets(updated_since=…)` |
| "Summarise the conversation on ticket 4512" | `get_ticket(4512)`, then `list_ticket_conversations` if `conversations_truncated` |
| "Who is +91 98xxxxxx21?" | `find_contacts(phone=…)` |
| "How many open tickets does Brewhouse Cafes have, and are they at risk?" | `find_companies` → `get_company` → `list_tickets(company_id=…)` |
| "Is the connector working / whose access is it using?" | `connector_status` |

The data comes back shaped for an LLM:
- Status, priority and source are words (`"pending"`), not Freshdesk's numeric codes.
- HTML bodies are converted to text and capped (2,000 chars by default).
- Every list result says `has_more` / `next_page`, so the agent knows when it has only part of the data.
- Private notes are labelled `private_note: true`. The server instructions tell the model never to quote them to the end customer.
- PII masking (emails and phone numbers) can be switched on with `FRESHDESK_REDACT_PII=true`.

## Cannot do (and why)

| Limitation | Reason | Workaround |
|---|---|---|
| Write anything: reply, update status, add a note, assign | Out of scope by design. Read-only is the safe default for a first deployment. | Add write tools later behind human approval (see README, "Long-term"). |
| Full-text search of ticket subjects or bodies ("tickets mentioning 'courier'") | Freshdesk's search API filters on fields only; it has no keyword search. | Filter by tag, type or date, then let the model scan the results. |
| See more than 300 search results for one query | Freshdesk caps search at 10 pages × 30. | The tool returns `total_matches` and a warning; narrow the query with date ranges. |
| See tickets older than 30 days with a plain `list_tickets` | That's Freshdesk's default list window. | Pass `updated_since`. The tool response includes a reminder note. |
| See tickets the API key's agent can't see | Freshdesk applies the agent's role and group scopes to the key. | Use a key belonging to an agent with "all tickets" scope, ideally a dedicated integration agent. |
| Download attachments | Only file name, type and size are returned, to keep payloads small and PII exposure low. | Possible future `get_attachment` tool. |
| Read Solutions (KB articles), canned responses, time entries, satisfaction ratings | Not needed for v1 support use cases. | Same pattern; each is roughly 20 lines in `service.py` plus a tool. |
| Real-time updates | Pull only. | Freshdesk webhooks or automations → Agent Studio trigger (long-term). |

## Behaviour under rate limits

Freshdesk limits are **per account per minute**: 50 calls/min on trial plans and up to about 700 on Enterprise. The limit is shared with every other app the merchant has installed. The connector:

1. learns the real limit from the `X-RateLimit-Total` header;
2. allows itself only 80% of it (`FRESHDESK_RATE_RESERVE=0.2`), so the merchant's other integrations keep working;
3. waits client-side when that budget is used up, and honours `Retry-After` on a 429;
4. never blocks a tool call for more than `FRESHDESK_MAX_WAIT_S` (20s by default). Past that limit it returns
   `{"error": "rate_limited", "retry_after_seconds": 42, "hint": "Wait about 42s …"}`, so the agent can tell the user instead of hanging.

Transient 5xx and network errors are retried with exponential backoff. Retrying is safe because every call is a GET.

## Error contract

Every failure is an MCP tool error whose text is JSON:

| `error` | Meaning | What the agent should do |
|---|---|---|
| `auth_failed` | Key invalid or revoked | Stop; ask the merchant to re-authenticate |
| `permission_denied` | Key's agent can't see that record | Tell the user; don't retry |
| `not_found` | Bad id | Use a search or find tool |
| `invalid_request` | Bad arguments | Fix them per `message` and retry |
| `rate_limited` | Quota exhausted | Wait `retry_after_seconds` |
| `upstream_unavailable` | Freshdesk down | Retry once later, then tell the user |
| `not_configured` | No credentials | Run `freshdesk-connector auth login` |
