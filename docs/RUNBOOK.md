# Runbook

## Deploy (hosted mode)

```bash
docker build -t freshdesk-connector:0.3.0 .
docker run -d -p 8000:8000 --read-only --tmpfs /tmp --cap-drop ALL \
  -e REDIS_URL=rediss://user:pass@redis:6379/0 \
  -e METRICS_TOKEN_FILE=/run/secrets/metrics_token \
  -e MCP_PUBLIC_URL=https://freshdesk-mcp.example.com \
  -e MCP_ALLOWED_HOSTS=freshdesk-mcp.example.com \
  -v /etc/freshdesk-connector/tenants.json:/etc/freshdesk-connector/tenants.json:ro \
  -v /run/secrets:/run/secrets:ro \
  freshdesk-connector:0.3.0
```

- Replicas are stateless; scale horizontally behind a load balancer. With more than one replica, `REDIS_URL` is **required**, or each replica assumes it has the merchant's whole quota.
- Probes: liveness `GET /healthz`; readiness `GET /readyz`. Readiness returns 503 if any tenant's key is missing or Redis is unreachable.
- Logs: JSON on stderr (`LOG_FORMAT=json`), one `tool_call` audit line per call.
- `scripts/smoke_compose.sh` runs the whole production shape locally and in CI.

## Onboard a merchant

1. In the merchant's Freshdesk, create a **dedicated integration agent**, restricted to the groups the use case needs, and copy its API key (Profile settings → View API key).
2. Store the key in the secret store and mount it, for example `/run/secrets/fd_key_<merchant>`.
3. Add the tenant to the registry:
   ```json
   "acme": {"domain": "acme", "api_key_file": "/run/secrets/fd_key_acme",
            "private_notes": "exclude", "redact_pii": false}
   ```
   For a customer-facing agent, keep `private_notes: exclude`. Use `include` only for internal support copilots.
4. Mint the agent's token: `freshdesk-connector token create --tenant acme --name agent-studio-acme-prod`. Put the printed token in Agent Studio's secret store and the printed `{"sha256": ...}` line in the registry's `tokens`.
5. Save the registry. Replicas pick up the change within ~5 s (no redeploy). Check `/readyz`, then call `connector_status` with the new token. It should show the merchant's domain, the integration agent's name and the account's statuses.

## Rotate

| What | How | Downtime |
|---|---|---|
| Agent Studio token | Mint a new token and add its hash next to the old one; switch Agent Studio to the new token; remove the old hash. Each registry edit applies within ~5 s | none |
| Freshdesk API key | Reset the key in Freshdesk (the old one dies immediately) and update the secret. The connector re-reads it on the next call and rebuilds the client | seconds; `auth_failed` until the secret is updated |
| Metrics token | Update the secret and the scraper config together | scrape gap only |

## Alerts (PromQL)

| Alert | Expression | Meaning / action |
|---|---|---|
| Auth failures | `sum by (tenant) (rate(fdconn_tool_calls_total{outcome="auth_failed"}[5m])) > 0` | Merchant key reset or revoked. Ask the merchant for a new key and rotate. |
| Rate limited | `sum by (tenant) (rate(fdconn_tool_calls_total{outcome="rate_limited"}[10m])) / sum by (tenant) (rate(fdconn_tool_calls_total[10m])) > 0.1` | Agent traffic exceeds 80% of the plan. Check for agent loops in the audit log; consider a lower `per_page` or `max_conversations`, or the merchant upgrading their plan. |
| Upstream failing | `rate(fdconn_tool_calls_total{outcome="upstream_unavailable"}[5m]) > 0` | Freshdesk is down or unreachable. Check status.freshdesk.com and egress. |
| Bugs | `rate(fdconn_tool_calls_total{outcome="internal_error"}[5m]) > 0` | Look up the exception in the logs (`level=error`) and roll back if new. |
| Latency | `histogram_quantile(0.95, sum by (le, tool) (rate(fdconn_tool_latency_seconds_bucket[5m]))) > 5` | Usually rate-limit waits or slow Freshdesk responses. |
| Injection attempts | `sum by (tenant) (increase(fdconn_guardrail_events_total{kind="prompt_injection_flagged"}[1h])) > 5` | Someone is probing the agent through tickets. Tell the merchant; review the transcripts. |

## Incident playbook

- **"The agent says the helpdesk is unavailable."** Check the `outcome` mix for that tenant in metrics. `rate_limited` → a quota issue (see above). `auth_failed` → key rotation. `upstream_unavailable` → Freshdesk side.
- **"The agent gave a wrong answer."** Find the `tool_call` audit lines around that time (tenant, tool, masked arguments, credits). Replay the same tool calls with the oracle (`python -m evals.run --oracle`), or add the conversation as a new eval case so it stays fixed.
- **Suspected data exposure.** Revoke the token by removing its hash from the registry. It stops working within ~5 s on every replica, with no redeploy. A malformed edit is rejected and the last good registry stays active (`/readyz` shows `registry_reload_error`). The audit log shows every call made with that tenant's tokens.
