# Architecture

```
Agent Studio agent ──MCP (stdio)──▶ connector ──HTTPS + Basic ──▶ shop.example.com/wp-json/wc/v3
                                      │                   or OAuth 1.0a (plain HTTP stores)
                                      │ typed filters · request budget
                                      │ guardrails · audit + metrics
```

## Request path

1. **`mcp_server._call`** wraps every tool: resets per-call counters, opens an
   audit span, and converts any exception into `{error, message, hint}` JSON.
   The model never sees a traceback.
2. **`query.py`** turns typed arguments into `wc/v3` parameters against an
   allow-list. The model cannot write query syntax, so a prompt-injected
   `role=administrator` never becomes a request.
3. **`ratelimit.py`** reserves budget before the call and settles it after,
   learning the store's real limit from `RateLimit-*` headers if it sends any.
4. **`client.py`** issues the GET, handles `429`/`503`/`5xx`/network, and
   refuses redirects so a misconfigured store cannot forward the credential
   to another host.
5. **`insights.py`** derives signals locally. No network, no model.
6. **`normalize.py`** shrinks the payload, masks PII and attaches a citation.
7. **`guardrails.py`** flags customer-authored text and enforces the response
   size budget by binary-searching the largest list prefix that fits.

## Why the rate limiter is client-side

WooCommerce core does not rate limit the REST API. Throttling comes from the
host (WP Engine, Kinsta, Cloudflare) as `429` or `503`, or from the Store API
if a merchant enabled it. The dangerous case is the third one: nothing
throttles, and an agent paging through orders takes a shared-hosting store
down. So the budget is enforced here regardless of what the store says, with a
20% reserve left for the merchant's other integrations, and raised only when
the store advertises a real limit.

## Why OAuth 1.0a is implemented at all

`WC_REST_Authentication::authenticate()` only attempts Basic auth when
`is_ssl()` is true, then falls through to OAuth. A plain-HTTP store therefore
authenticates a key/secret query string as *nobody*, and every call returns
`woocommerce_rest_cannot_view`, which looks like a permissions problem. Since
local development stores are the normal plain-HTTP case, and since the
assignment asked for an auth flow, both are implemented and the transport
picks between them.

The signature construction mirrors WooCommerce's own, including two places it
departs from the spec: each `key=value` pair is RFC 3986 encoded as a whole
string and the pairs joined with `%26`, and the signing key is
`consumer_secret + "&"` with no token secret.

## Known gaps and the shape of the fix

| Gap | Fix |
|---|---|
| Rate budget is in-process, so replicas each think they have the whole budget | Move `RateLimiter` behind the same async interface backed by Redis, with check-and-reserve as one atomic script using server time |
| One store per process | A tenant registry keyed by hashed bearer token, resolving the tenant server-side so it can never come from a tool argument |
| Pull only | WooCommerce webhooks into an Agent Studio trigger, with HMAC verification |
| References found but not resolved | A Razorpay Payments/Refunds tool, so `reconciliation` reports what the gateway says rather than where to look |
