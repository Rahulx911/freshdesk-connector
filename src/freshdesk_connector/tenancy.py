"""Hosted, multi-merchant mode.

Security model: every Agent Studio agent gets its own bearer token, and each
token is bound to exactly ONE merchant (tenant). The tenant is derived from
the token on the server side, never from anything the model or client
sends, so an agent cannot read another merchant's helpdesk even if it is
prompt-injected into asking for one.

Registry file (JSON, safe to keep in config management, holds no secrets):

{
  "tenants": {
    "kettle-and-leaf": {
      "domain": "kettleandleaf",                    # or full https URL
      "api_key_env": "FD_KEY_KETTLE_AND_LEAF",      # or "api_key_file": "/run/secrets/fd_kl"
      "redact_pii": false,
      "private_notes": "exclude"                    # exclude | include
    }
  },
  "tokens": [
    {"name": "agent-studio-kl-prod", "tenant": "kettle-and-leaf",
     "sha256": "<hex sha256 of the bearer token>"}
  ]
}

Only SHA-256 hashes of tokens are stored; `freshdesk-connector token create`
mints a token and prints the hash line to add here.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp.server.auth.provider import AccessToken

from .auth import Credentials, normalize_base_url
from .errors import ConfigError

SCOPE = "freshdesk:read"
log = logging.getLogger("freshdesk_connector")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def mint_token() -> str:
    return "fdc_" + secrets.token_urlsafe(32)


@dataclass(frozen=True)
class TenantConfig:
    tenant_id: str
    domain: str
    api_key_env: str | None = None
    api_key_file: str | None = None
    redact_pii: bool = False
    private_notes: str = "exclude"

    def credentials(self) -> Credentials:
        if self.api_key_env:
            key = os.environ.get(self.api_key_env)
            if not key:
                raise ConfigError(f"Tenant {self.tenant_id}: env var {self.api_key_env} is not set")
        elif self.api_key_file:
            try:
                key = Path(self.api_key_file).read_text().strip()
            except OSError as e:
                raise ConfigError(f"Tenant {self.tenant_id}: cannot read api_key_file") from e
        else:
            raise ConfigError(f"Tenant {self.tenant_id}: needs api_key_env or api_key_file")
        return Credentials(base_url=normalize_base_url(self.domain), api_key=key)


@dataclass
class TenantRegistry:
    tenants: dict[str, TenantConfig]
    token_hashes: dict[str, str] = field(default_factory=dict)   # sha256 -> tenant_id
    token_names: dict[str, str] = field(default_factory=dict)    # sha256 -> token name

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TenantRegistry:
        tenants: dict[str, TenantConfig] = {}
        for tid, cfg in (data.get("tenants") or {}).items():
            if cfg.get("api_key"):
                raise ConfigError(f"Tenant {tid}: inline api_key is not allowed; use api_key_env or api_key_file")
            pn = cfg.get("private_notes", "exclude")
            if pn not in ("exclude", "include"):
                raise ConfigError(f"Tenant {tid}: private_notes must be 'exclude' or 'include'")
            tenants[tid] = TenantConfig(
                tenant_id=tid, domain=cfg["domain"], api_key_env=cfg.get("api_key_env"),
                api_key_file=cfg.get("api_key_file"), redact_pii=bool(cfg.get("redact_pii", False)),
                private_notes=pn,
            )
            normalize_base_url(cfg["domain"])          # validate early
        hashes, names = {}, {}
        for t in data.get("tokens") or []:
            h = t["sha256"].lower()
            if len(h) != 64 or any(c not in "0123456789abcdef" for c in h):
                raise ConfigError(f"Token {t.get('name')}: sha256 must be 64 hex chars")
            if t["tenant"] not in tenants:
                raise ConfigError(f"Token {t.get('name')}: unknown tenant {t['tenant']}")
            hashes[h] = t["tenant"]
            names[h] = t.get("name", "unnamed")
        return cls(tenants=tenants, token_hashes=hashes, token_names=names)

    @classmethod
    def load(cls, path: str | os.PathLike) -> TenantRegistry:
        try:
            return cls.from_dict(json.loads(Path(path).read_text()))
        except (OSError, json.JSONDecodeError, KeyError) as e:
            raise ConfigError(f"Invalid tenant registry {path}: {e}") from e

    def tenant_for_token(self, token: str) -> str | None:
        h = hash_token(token)
        # constant-time compare against every known hash (no early exit on match)
        found = None
        for known, tenant in self.token_hashes.items():
            if hmac.compare_digest(known, h):
                found = tenant
        return found


class RegistryWatcher:
    """Serves the current registry and re-reads the file when it changes (checked at most
    every `interval_s`), so revoking a token or onboarding a merchant needs no redeploy:
    edit the file (e.g. a ConfigMap update) and the change applies within seconds.
    A broken edit never takes the service down: the last good registry stays active."""

    def __init__(self, path: str | os.PathLike, interval_s: float = 5.0, clock: Any = time.monotonic):
        self.path = Path(path)
        self.interval_s = interval_s
        self.clock = clock
        self._registry = TenantRegistry.load(self.path)     # fail fast at startup
        self._mtime = self._stat()
        self._next_check = clock() + interval_s
        self.reloads = 0
        self.last_error: str | None = None

    def _stat(self) -> float:
        try:
            return self.path.stat().st_mtime_ns
        except OSError:
            return -1

    def current(self) -> TenantRegistry:
        now = self.clock()
        if now >= self._next_check:
            self._next_check = now + self.interval_s
            mtime = self._stat()
            if mtime != self._mtime:
                try:
                    self._registry = TenantRegistry.load(self.path)
                    self._mtime = mtime
                    self.reloads += 1
                    self.last_error = None
                    log.info("tenant registry reloaded", extra={"event": {
                        "tenants": len(self._registry.tenants), "tokens": len(self._registry.token_hashes)}})
                except ConfigError as e:
                    self.last_error = str(e)
                    log.error("tenant registry reload failed; keeping previous version: %s", e)
        return self._registry


class RegistryTokenVerifier:
    """MCP TokenVerifier: bearer token -> AccessToken whose client_id is the tenant."""

    def __init__(self, registry: TenantRegistry | RegistryWatcher):
        self.source = registry

    @property
    def registry(self) -> TenantRegistry:
        return self.source.current() if isinstance(self.source, RegistryWatcher) else self.source

    async def verify_token(self, token: str) -> AccessToken | None:
        tenant = self.registry.tenant_for_token(token)
        if tenant is None:
            return None
        return AccessToken(token=hash_token(token)[:12], client_id=tenant, scopes=[SCOPE])
