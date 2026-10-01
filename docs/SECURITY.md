# Security

## What is worth stealing

1. **The merchant's consumer key and secret.** They grant read access to every
   order, which means names, emails, phones and addresses.
2. **The order data itself**, which is personal data under Indian DPDP and the
   GDPR for any EU customers.

## Controls

### The credential cannot write
WooCommerce scopes keys at creation. The connector asks for **Read**. Verified
against a real store:

```
POST /wp-json/wc/v3/orders
401 woocommerce_rest_authentication_error
    "The API key provided does not have write permissions."
```

This is the store refusing, independently of this code. A write bug here is
not exploitable.

### The secret never travels in clear
`normalize_store_url` refuses plain HTTP for any non-local host, so the secret
cannot be sent unencrypted to a real store. Local development hosts
(`localhost`, `127.0.0.1`, `*.localhost`, `*.test`) are the only exception, and
there the credential is never sent at all: OAuth 1.0a sends a signature.

### SSRF
Store URLs are validated before any request: https only, no raw IP addresses,
no cloud metadata hostnames, no single-label internal names. Redirects are
refused rather than followed, so a store that redirects to another host cannot
capture the Authorization header.

### Credentials at rest
Written with `O_CREAT|O_WRONLY|O_TRUNC` at mode `0600` from the first byte,
never world-readable even briefly. The environment takes precedence, so
containers never need the file. `redacted()` is the only representation that
leaves the process, and a test asserts the secret cannot appear in it.

### Logs and tool output
Every tool call emits one audit line with arguments masked: emails, long digit
runs and anything shaped like a `ck_`/`cs_` key. A test asserts that no
credential appears in any tool response.

### Prompt injection
Order notes and refund reasons are customer-authored and reach the model.
They are flagged, not stripped, with `untrusted_text_flags` and a note telling
the model to treat the text as data. The server instructions repeat it. The
deeper mitigation is structural: the model cannot write query syntax, and the
connector cannot write to the store, so a successful injection still cannot
move money.

### PII
Masked by default: email keeps the domain only, phone keeps the last four
digits, names reduce to a first name and initials. Customer IP and user agent
are never returned. Masking is off only when a deployment sets it off.

## Residual risks

- A Read key still reads **every** order. WooCommerce has no per-group key
  scoping, so least privilege stops at Read.
- Masked data is still linkable. A domain plus a postcode plus an order total
  can identify someone in a small store.
- The audit log records tool arguments. If a merchant's own log pipeline is
  insecure, that is a second copy of the metadata.
