"""Binding one Agent Studio agent to exactly one merchant.

Agent Studio serves many merchants from one deployment. The security
property that matters is simple to state and easy to get wrong:

    **The tenant is derived from the bearer token, server side. It is never
    read from a tool argument.**

If a tenant id were an argument, a prompt injection in a customer's order
note could ask the agent to pass a different one, and the agent would
comply, because that is what agents do with instructions in their context.
Deriving it from the credential makes that attack structurally impossible
rather than something guardrails have to catch.

Registry shape:

    {
      "tenants": {
        "kettle-and-leaf": {
          "store_url": "https://kettleandleaf.example.com",
          "consumer_key_env": "WOO_CK_KETTLE",
          "consumer_secret_env": "WOO_CS_KETTLE",
          "razorpay_key_id_env": "RZP_ID_KETTLE",
          "razorpay_key_secret_env": "RZP_SECRET_KETTLE",
          "redact_pii": true
        }
      },
      "tokens": [
        {"name": "support-agent", "tenant": "kettle-and-leaf",
         "sha256": "<hex sha256 of the bearer token>"}
      ]
    }

Only token *hashes* are stored. `woocommerce-connector token create` mints a
token, prints it once, and prints the registry line to paste. Secrets are
referenced by environment variable or file, never inlined, and an inline
value is rejected rather than warned about.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

from .auth import Credentials, normalize_store_url
from .errors import AuthError, ConfigError

ENV_REGISTRY = "WOO_TENANTS_FILE"
TOKEN_PREFIX = "wcc_"  # noqa: S105  # nosec B105


def mint_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _read_secret(env_name: str | None, file_name: str | None, label: str) -> str:
    if env_name:
        value = os.environ.get(env_name)
        if not value:
            raise ConfigError(f"{label}: environment variable {env_name} is not set")
        return value
    if file_name:
        try:
            return Path(file_name).read_text().strip()
        except OSError as e:
            raise ConfigError(f"{label}: cannot read {file_name}") from e
    raise ConfigError(f"{label}: needs an _env or _file reference")


@dataclass(frozen=True)
class Tenant:
    tenant_id: str
    store_url: str
    consumer_key_env: str | None = None
    consumer_key_file: str | None = None
    consumer_secret_env: str | None = None
    consumer_secret_file: str | None = None
    razorpay_key_id_env: str | None = None
    razorpay_key_secret_env: str | None = None
    redact_pii: bool = True

    def credentials(self) -> Credentials:
        key = _read_secret(self.consumer_key_env, self.consumer_key_file,
                           f"Tenant {self.tenant_id} consumer key")
        secret = _read_secret(self.consumer_secret_env, self.consumer_secret_file,
                              f"Tenant {self.tenant_id} consumer secret")
        return Credentials(normalize_store_url(self.store_url), key, secret)

    def razorpay_credentials(self):
        """Optional: a tenant may have the store connected but not the gateway."""
        if not (self.razorpay_key_id_env and self.razorpay_key_secret_env):
            return None
        from razorpay_connector.auth import Credentials as RzpCredentials
        key_id = _read_secret(self.razorpay_key_id_env, None,
                              f"Tenant {self.tenant_id} Razorpay key id")
        key_secret = _read_secret(self.razorpay_key_secret_env, None,
                                  f"Tenant {self.tenant_id} Razorpay key secret")
        return RzpCredentials(key_id, key_secret)


@dataclass
class Registry:
    tenants: dict[str, Tenant] = field(default_factory=dict)
    token_hashes: dict[str, str] = field(default_factory=dict)   # sha256 -> tenant_id
    token_names: dict[str, str] = field(default_factory=dict)    # sha256 -> token name

    @classmethod
    def from_dict(cls, raw: dict) -> Registry:
        tenants: dict[str, Tenant] = {}
        for tid, cfg in (raw.get("tenants") or {}).items():
            for forbidden in ("consumer_key", "consumer_secret",
                              "razorpay_key_id", "razorpay_key_secret"):
                if cfg.get(forbidden):
                    raise ConfigError(
                        f"Tenant {tid}: inline {forbidden} is not allowed; "
                        f"use {forbidden}_env or {forbidden}_file"
                    )
            if not cfg.get("store_url"):
                raise ConfigError(f"Tenant {tid}: store_url is required")
            tenants[tid] = Tenant(
                tenant_id=tid,
                store_url=cfg["store_url"],
                consumer_key_env=cfg.get("consumer_key_env"),
                consumer_key_file=cfg.get("consumer_key_file"),
                consumer_secret_env=cfg.get("consumer_secret_env"),
                consumer_secret_file=cfg.get("consumer_secret_file"),
                razorpay_key_id_env=cfg.get("razorpay_key_id_env"),
                razorpay_key_secret_env=cfg.get("razorpay_key_secret_env"),
                redact_pii=bool(cfg.get("redact_pii", True)),
            )

        hashes: dict[str, str] = {}
        names: dict[str, str] = {}
        for entry in raw.get("tokens") or []:
            digest = (entry.get("sha256") or "").strip().lower()
            tenant_id = entry.get("tenant")
            if not digest or len(digest) != 64:
                raise ConfigError(f"Token entry {entry.get('name')!r}: sha256 must be 64 hex chars")
            if tenant_id not in tenants:
                raise ConfigError(
                    f"Token entry {entry.get('name')!r} references unknown tenant {tenant_id!r}")
            hashes[digest] = tenant_id
            names[digest] = entry.get("name") or "unnamed"
        return cls(tenants=tenants, token_hashes=hashes, token_names=names)

    @classmethod
    def load(cls, path: str | Path | None = None) -> Registry:
        location = Path(path or os.environ.get(ENV_REGISTRY, ""))
        if not location or not location.exists():
            raise ConfigError(
                f"No tenant registry. Set {ENV_REGISTRY} to a readable JSON file.")
        try:
            return cls.from_dict(json.loads(location.read_text()))
        except ValueError as e:
            raise ConfigError(f"Tenant registry is not valid JSON: {e}") from e

    def resolve(self, token: str) -> Tenant:
        """Map a bearer token to its tenant, in constant time.

        A plain dict lookup on the digest would leak nothing useful, but the
        comparison is done with compare_digest anyway so the shape of this
        function cannot regress into a timing oracle later.
        """
        if not token:
            raise AuthError("No bearer token presented")
        digest = hash_token(token)
        for known, tenant_id in self.token_hashes.items():
            if hmac.compare_digest(known, digest):
                return self.tenants[tenant_id]
        raise AuthError("Bearer token is not recognised")

    def describe(self) -> dict:
        """Safe to log: counts and ids, never a hash or a secret."""
        return {
            "tenants": sorted(self.tenants),
            "tenant_count": len(self.tenants),
            "token_count": len(self.token_hashes),
        }


class RegistryWatcher:
    """Re-reads the registry when the file changes, so revoking a token does
    not require a restart. Checked at most once every `interval_s`."""

    def __init__(self, path: str | Path | None = None, interval_s: float = 5.0):
        self.path = Path(path or os.environ.get(ENV_REGISTRY, ""))
        self.interval_s = interval_s
        self._registry: Registry | None = None
        self._loaded_mtime: float | None = None
        self._checked_at = 0.0

    def get(self, *, now: float | None = None) -> Registry:
        current = now if now is not None else time.monotonic()
        if self._registry is None or current - self._checked_at >= self.interval_s:
            self._checked_at = current
            try:
                mtime = self.path.stat().st_mtime
            except OSError as e:
                if self._registry is None:
                    raise ConfigError(f"Cannot read tenant registry at {self.path}") from e
                return self._registry          # keep serving the last good copy
            if mtime != self._loaded_mtime:
                self._registry = Registry.load(self.path)
                self._loaded_mtime = mtime
        assert self._registry is not None
        return self._registry
