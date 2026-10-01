# Runbook

## Connecting a merchant

1. In the merchant's WordPress: **WooCommerce → Settings → Advanced → REST
   API → Add key**. Description "Agent Studio connector", user = a shop
   manager, **Permissions = Read**.
2. Copy the consumer key and secret. WooCommerce shows them once.
3. `woocommerce-connector auth login` and paste them. The key is verified
   against the store before anything is written to disk.
4. `woocommerce-connector auth status` should report the store and the number
   of visible orders.

Never accept a Read-Write key. If the merchant sends one, ask for a Read key
and tell them to revoke the first.

## Symptoms

| Symptom | Cause | Action |
|---|---|---|
| `auth_failed` on every call | Key revoked, or regenerated in wp-admin | Re-run `auth login` with a new Read key |
| `permission_denied: cannot list resources` on a plain-HTTP store | Basic auth is ignored without TLS | Expected; the connector signs with OAuth 1.0a. If it persists, the key is wrong |
| `permission_denied` on an HTTPS store | The key's WordPress user lacks shop capabilities | Have the merchant attach the key to a shop manager or administrator |
| `rate_limited` with a large `retry_after_seconds` | The host is throttling, or the budget is exhausted | Tell the user; do not retry in a loop. Check `connector_status` |
| `upstream_unavailable` | Store down, or slow enough to exceed the time budget | Check the store directly. Retry once later |
| `invalid_request: Store redirected the API request` | The configured URL is not canonical (`www`, or `http` → `https`) | Reconfigure with the exact canonical URL |
| Every citation link 404s | HPOS mismatch | Flip `hpos_admin_links`; WooCommerce 8.2+ stores are HPOS |

## Rotating a key

Regenerating in wp-admin kills the old key immediately. Rotate the secret
first, then `auth login`; expect `auth_failed` in the gap.

## Local store for testing

```bash
cd deploy/woo-local
docker compose up -d && ./bootstrap.sh      # prints a Read-scoped key
docker compose down -v                      # removes everything
```

Real WordPress and WooCommerce, seeded with fictional data. Nothing in it is
meant to face the internet; the admin password is fixed deliberately because
it is a disposable fixture.
