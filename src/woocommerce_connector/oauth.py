"""OAuth 1.0a one-legged signing, as WooCommerce actually implements it.

WooCommerce accepts HTTP Basic auth **only over HTTPS**. Its
`WC_REST_Authentication::authenticate()` is literally:

    if ( is_ssl() ) { $user_id = $this->perform_basic_authentication(); }
    if ( $user_id ) { return $user_id; }
    return $this->perform_oauth_authentication();

So a store reached over plain HTTP, which is the normal case for a local
development store, requires a signed request. Sending the consumer key and
secret as query parameters over HTTP silently authenticates as nobody and
every call comes back `woocommerce_rest_cannot_view`, which looks like a
permissions problem rather than an auth one. That is a genuinely confusing
failure, so we implement the signature properly.

The construction below mirrors WooCommerce's `check_oauth_signature()`
exactly, including the two details where it departs from the OAuth 1.0a spec
and from most client libraries:

* each `key=value` pair is RFC 3986 encoded **as a whole string**, so the
  `=` becomes `%3D` and any percent sign in the value becomes `%25`, and the
  pairs are then joined with `%26` rather than `&`;
* the signing key is `consumer_secret + "&"` with no token secret.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from typing import Any
from urllib.parse import quote

SIGNATURE_METHOD = "HMAC-SHA256"
_ALGORITHMS = {"HMAC-SHA256": hashlib.sha256, "HMAC-SHA1": hashlib.sha1}


def rfc3986(value: Any) -> str:
    """WooCommerce's `wc_rest_urlencode_rfc3986`.

    `rawurlencode` with `%7E` mapped back to `~`. Python's `quote` already
    treats `~` as unreserved, so `safe=""` reproduces it.
    """
    return quote(str(value), safe="")


def signature_base_string(method: str, url: str, params: dict[str, Any]) -> str:
    """Build the exact string WooCommerce will hash on its side."""
    # Sorted on the raw keys, matching PHP's uksort(..., 'strcmp').
    ordered = sorted((str(k), v) for k, v in params.items() if k != "oauth_signature")
    # Normalize key and value separately, then encode the joined pair again.
    pairs = [rfc3986(f"{rfc3986(k)}={rfc3986(v)}") for k, v in ordered]
    query_string = "%26".join(pairs)
    return f"{method.upper()}&{rfc3986(url)}&{query_string}"


def sign(
    method: str,
    url: str,
    params: dict[str, Any],
    consumer_key: str,
    consumer_secret: str,
    *,
    signature_method: str = SIGNATURE_METHOD,
    timestamp: int | None = None,
    nonce: str | None = None,
) -> dict[str, Any]:
    """Return `params` plus the oauth_* fields, ready to send as a query string."""
    if signature_method not in _ALGORITHMS:
        raise ValueError(f"unsupported signature method {signature_method!r}")

    signed: dict[str, Any] = dict(params)
    signed.update({
        "oauth_consumer_key": consumer_key,
        "oauth_timestamp": str(timestamp if timestamp is not None else int(time.time())),
        "oauth_nonce": nonce or secrets.token_hex(16),
        "oauth_signature_method": signature_method,
    })

    base = signature_base_string(method, url, signed)
    digest = hmac.new(
        (consumer_secret + "&").encode(),
        base.encode(),
        _ALGORITHMS[signature_method],
    ).digest()
    signed["oauth_signature"] = base64.b64encode(digest).decode()
    return signed
