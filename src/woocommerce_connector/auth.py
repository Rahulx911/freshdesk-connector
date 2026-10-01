"""Credentials for the WooCommerce REST API.

WooCommerce issues a **consumer key / consumer secret** pair per API key, and
each key is scoped at creation time to Read, Write or Read-Write. This
connector asks the merchant for a **Read** key, which is a stronger guarantee
than a convention in our code: even a bug here cannot write to the store,
because the credential itself has no write permission.

Transport rules (WooCommerce's own):

* Over **HTTPS** the key/secret go in HTTP Basic auth.
* Over plain **HTTP** WooCommerce requires OAuth 1.0a one-legged. We do not
  implement that. Instead, plain HTTP is refused unless the host is an
  explicit local development host, which keeps `http://localhost:8080` usable
  for a real local WooCommerce while never sending a live merchant's secret
  in clear text.

URL validation is deliberately strict (SSRF): https only, no raw IP
addresses, no internal/metadata hostnames.
"""

from __future__ import annotations

import ipaddress
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from .errors import ConfigError

ENV_URL = "WOO_STORE_URL"
ENV_KEY = "WOO_CONSUMER_KEY"
ENV_SECRET = "WOO_CONSUMER_SECRET"  # noqa: S105  # nosec B105

API_NAMESPACE = "/wp-json/wc/v3"

# Hosts that may be reached over plain HTTP, for a local development store.
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}
_LOCAL_SUFFIXES = (".localhost", ".test")

# Never reachable: cloud metadata and link-local.
_BLOCKED_HOSTS = {"metadata.google.internal", "metadata", "instance-data"}


def config_home() -> Path:
    base = os.environ.get("WOO_CONNECTOR_HOME") or os.path.join(
        os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "woocommerce-connector"
    )
    return Path(base)


def _creds_path() -> Path:
    return config_home() / "credentials.json"


def is_local_host(host: str) -> bool:
    h = host.lower()
    return h in _LOCAL_HOSTS or h.endswith(_LOCAL_SUFFIXES)


def normalize_store_url(raw: str) -> str:
    """Validate and normalise a store URL to its scheme://host[:port] root.

    Raises ConfigError for anything that is not a plausible public store, or a
    local development host over http.
    """
    value = (raw or "").strip()
    if not value:
        raise ConfigError("Store URL is empty")
    if "://" not in value:
        value = "https://" + value
    parsed = urlparse(value)
    scheme, host = parsed.scheme.lower(), (parsed.hostname or "").lower()
    if not host:
        raise ConfigError(f"Could not read a hostname from {raw!r}")
    if host in _BLOCKED_HOSTS:
        raise ConfigError(f"Refusing to talk to internal host {host!r}")

    local = is_local_host(host)
    if scheme not in ("http", "https"):
        raise ConfigError(f"Unsupported scheme {scheme!r}; use https")
    if scheme == "http" and not local:
        raise ConfigError(
            "Refusing to send the consumer secret over plain HTTP. Use https, or a local "
            "development host (localhost, 127.0.0.1, *.localhost, *.test)."
        )

    if not local:
        try:
            ip = ipaddress.ip_address(host.strip("[]"))
        except ValueError:
            ip = None
        if ip is not None:
            raise ConfigError("Refusing to talk to a raw IP address; use the store's hostname")
        if "." not in host:
            raise ConfigError(f"{host!r} does not look like a public hostname")

    netloc = host if parsed.port is None else f"{host}:{parsed.port}"
    return urlunparse((scheme, netloc, "", "", "", ""))


@dataclass(frozen=True)
class Credentials:
    store_url: str
    consumer_key: str
    consumer_secret: str

    @property
    def host(self) -> str:
        return urlparse(self.store_url).hostname or ""

    @property
    def uses_basic_auth(self) -> bool:
        """HTTPS -> Basic auth. Local http -> query string (WooCommerce accepts both)."""
        return self.store_url.startswith("https://")

    def api_base(self) -> str:
        return self.store_url + API_NAMESPACE

    @property
    def auth_scheme(self) -> str:
        """HTTPS -> Basic auth. Plain HTTP -> OAuth 1.0a one-legged signing,
        which is the only thing WooCommerce accepts without TLS."""
        return "basic" if self.uses_basic_auth else "oauth1"

    def redacted(self) -> dict:
        return {
            "store_url": self.store_url,
            "consumer_key": mask_key(self.consumer_key),
            "auth": "http-basic over https" if self.uses_basic_auth else "oauth 1.0a one-legged",
        }


def mask_key(key: str) -> str:
    """ck_1234...abcd -> ck_1234…cd (never log or return the whole key)."""
    if not key:
        return ""
    head = key[:7] if len(key) > 11 else key[:3]
    return f"{head}…{key[-2:]}" if len(key) > 11 else head + "…"


def save(creds: Credentials) -> Path:
    home = config_home()
    home.mkdir(parents=True, exist_ok=True)
    os.chmod(home, stat.S_IRWXU)
    path = _creds_path()
    payload = {
        "store_url": creds.store_url,
        "consumer_key": creds.consumer_key,
        "consumer_secret": creds.consumer_secret,
    }
    # Write 0600 from the start; never world-readable even briefly.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w") as fh:
        json.dump(payload, fh)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return path


def clear() -> bool:
    path = _creds_path()
    if path.exists():
        path.unlink()
        return True
    return False


def load() -> Credentials:
    """Environment first (containers, CI), then the 0600 on-disk store."""
    url, key, secret = (os.environ.get(ENV_URL), os.environ.get(ENV_KEY), os.environ.get(ENV_SECRET))
    if url and key and secret:
        return Credentials(normalize_store_url(url), key, secret)

    path = _creds_path()
    if not path.exists():
        raise ConfigError(
            "No WooCommerce credentials. Run `woocommerce-connector auth login`, or set "
            f"{ENV_URL}, {ENV_KEY} and {ENV_SECRET}."
        )
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise ConfigError(f"Could not read {path}: {e}") from e
    missing = [k for k in ("store_url", "consumer_key", "consumer_secret") if not data.get(k)]
    if missing:
        raise ConfigError(f"Stored credentials are incomplete (missing {', '.join(missing)})")
    return Credentials(normalize_store_url(data["store_url"]), data["consumer_key"], data["consumer_secret"])
