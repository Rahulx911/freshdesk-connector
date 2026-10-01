# Security model

## Assets

1. Merchants' Freshdesk API keys. These give read access to the merchant's whole helpdesk as the key's agent.
2. End-customer PII in tickets: names, emails, phone numbers, order and payment references.
3. Internal private notes.
4. Each merchant's Freshdesk rate-limit quota, which their other integrations also depend on.

## Threats and controls

| Threat | Control | Verified by |
|---|---|---|
| Agent of merchant A reads merchant B's data | Tenant derived only from the bearer token; tools have no tenant argument; one token maps to one tenant | `test_production.py::test_token_selects_merchant_and_isolates` |
| Unauthenticated access to hosted endpoint | MCP `RequireAuth` on `/mcp` (401 + `WWW-Authenticate`); the server refuses to bind a non-local interface without a tenant registry | `test_rejects_missing_and_wrong_tokens`, `test_refuses_public_http_without_auth`, compose smoke test |
| Token theft from config | Only SHA-256 hashes are stored; tokens are shown once at creation; constant-time comparison | `test_token_create_prints_hash_only_once`, `test_registry_validation` |
| Slow revocation | Registry hot-reload: a removed token stops working within ~5 s on every replica; a broken edit keeps the last good registry | `test_registry_hot_reload_revokes_without_restart`, `test_broken_registry_edit_keeps_last_good` |
| One bad tenant taking everyone down | Readiness is degraded per tenant rather than failing the replica; per-tenant credentials are resolved lazily | `test_key_rotation_rebuilds_client_and_readiness_is_per_tenant` |
| API key leakage | Keys come from env or secret files only (inline keys are rejected); never logged or returned; redacted `repr`; httpx URL logging off | `test_api_key_never_logged_or_returned`, `test_audit_log_is_json_and_has_no_secrets`, smoke test greps container logs |
| Prompt injection via ticket content | Content flagged (`possible_prompt_injection`); server instructions say to treat it as data; **no write tools exist**, so even a fully hijacked agent can only read | `test_flags_injection_attempts`, `test_no_false_positive_on_normal_support_text`, eval case `prompt-injection-in-ticket` |
| Search query injection | The model never writes query syntax; typed arguments, allow-listed characters, 512-char cap | `test_injection_and_garbage_inputs_never_reach_freshdesk` |
| Private notes reaching end customers | Withheld by default; opt-in per tenant | `test_get_ticket_full`, eval case `private-note-not-leaked` |
| PII in logs | Audit arguments masked (`m***@domain`, full RFC 5322 local parts); no response bodies logged | `test_audit_log_is_json_and_has_no_secrets`, `test_fuzz.py::test_mask_pii_hides_every_email` |
| Customer text sent to third parties | Intent and payment signals are computed locally by rules; nothing leaves the connector except to Freshdesk | `insights.py` (no network calls) |
| SSRF via tenant domain | https only; IP literals and internal suffixes (`.internal`, `.svc`, `.local`…) rejected; no paths; plain http only for loopback or an explicit test allowlist | `test_auth.py::test_normalize_rejects_bad_domains` |
| Context flooding / cost blow-up | Body truncation and a per-response size budget with explicit markers | `test_response_budget_truncates_with_marker` |
| Starving the merchant's other integrations | 20% quota reserve; budget shared across replicas through Redis; 429 handling | `test_replicas_share_one_budget_via_redis`, load test burst (0 upstream 429s) |
| DNS rebinding (hosted) | `--allowed-hosts` / `MCP_ALLOWED_HOSTS` turns on MCP transport security | config |
| Container escape / tampering | Non-root UID 10001, read-only root filesystem, all capabilities dropped, `no-new-privileges`, hash-pinned dependencies | `Dockerfile`, `docker-compose.yml` |
| Vulnerable dependencies | `pip-audit --strict` on the hash-pinned `requirements.lock` in CI; `bandit` on the source | CI `quality` job |

## Residual risks

- **Regex injection detection is heuristic.** Novel phrasings will get through. The real mitigation is architectural: the connector has no write tools and the agent can't change tenant. Flags are a signal for the agent and for monitoring (`fdconn_guardrail_events_total`), not a security boundary.
- **The API key's scope is the Freshdesk agent's scope.** Use a dedicated integration agent restricted to the groups the use case needs.
- **Redis is trusted.** In production use AUTH and TLS (`rediss://`) on a private network. A tampered budget could only throttle the connector; it can't read data.
- **`/metrics` shows tenant ids as labels.** Set `METRICS_TOKEN` (or `METRICS_TOKEN_FILE`) and scrape over the private network.

## Reporting

Report security issues privately to the maintainer (GitHub: @Rahulx911). Don't open a public issue.
