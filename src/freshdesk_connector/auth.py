"""API-key authentication for Freshdesk.

Freshdesk's REST API v2 authenticates with HTTP Basic auth where the
username is the agent's API key and the password is any placeholder
("X"). Freshdesk does not offer a merchant-facing OAuth grant for its own
REST API, so the flow here is:

    1. merchant pastes domain + API key   (`freshdesk-connector auth login`)
    2. we call GET /api/v2/agents/me to prove the key works and learn whose it is
    3. only then persist it, file mode 0600, outside the repo
    4. every request reads the credential from env first, then the store

The key is never logged, echoed back, or returned through an MCP tool.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from .errors import ConfigError

ENV_DOMAIN = "FRESHDESK_DOMAIN"
ENV_API_KEY = "FRESHDESK_API_KEY"


def default_store_path() -> Path:
    base = os.environ.get("FRESHDESK_CONNECTOR_HOME") or os.path.join(
        os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "freshdesk-connector"
    )
    return Path(base) / "credentials.json"


_SUBDOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$", re.I)


_INTERNAL_SUFFIXES = (".local", ".internal", ".localdomain", ".svc", ".cluster.local", ".lan", ".home")


def _insecure_hosts() -> set[str]:
    """Hosts allowed over plain http: loopback always (local mock), plus an explicit
    opt-in list for test stacks (e.g. FRESHDESK_INSECURE_HTTP_HOSTS=mock-freshdesk)."""
    extra = {h.strip().lower() for h in os.environ.get("FRESHDESK_INSECURE_HTTP_HOSTS", "").split(",") if h.strip()}
    return {"localhost", "127.0.0.1"} | extra


def normalize_base_url(domain: str) -> str:
    """Accept 'acme', 'acme.freshdesk.com', a custom helpdesk domain, or an explicit
    URL. Rejects IP literals and internal hostnames over https (SSRF hygiene: the
    connector should only ever talk to a public Freshdesk endpoint), and plain http
    except for loopback / explicitly allowed test hosts."""
    d = domain.strip().rstrip("/")
    if not d:
        raise ConfigError("Freshdesk domain is empty")
    m = re.match(r"^(https?)://([^/:?#]+)(:\d+)?$", d, re.I)
    if d.lower().startswith(("http://", "https://")):
        if not m:
            raise ConfigError(f"Freshdesk URL must be scheme://host[:port] with no path: {domain!r}")
        scheme, host = m.group(1).lower(), m.group(2).lower()
        if scheme == "http":
            if host not in _insecure_hosts():
                raise ConfigError("Plain http is only allowed for localhost (mock server) or hosts in "
                                  "FRESHDESK_INSECURE_HTTP_HOSTS")
            return d
        _check_public_host(host)
        return d
    if _SUBDOMAIN_RE.match(d):
        return f"https://{d}.freshdesk.com"
    if re.match(r"^[a-z0-9.-]+\.[a-z]{2,}$", d, re.I):
        _check_public_host(d.lower())
        return f"https://{d}"
    raise ConfigError(f"Not a valid Freshdesk domain: {domain!r}")


def _check_public_host(host: str) -> None:
    try:
        ipaddress.ip_address(host.strip("[]"))
        raise ConfigError("IP addresses are not allowed as a Freshdesk domain; use the helpdesk hostname")
    except ValueError:
        pass
    if host.endswith(_INTERNAL_SUFFIXES) or "." not in host:
        raise ConfigError(f"Internal hostname not allowed as a Freshdesk domain: {host!r}")


@dataclass(frozen=True)
class Credentials:
    base_url: str
    api_key: str
    agent_name: str | None = None
    agent_email: str | None = None

    def auth_header(self) -> str:
        token = base64.b64encode(f"{self.api_key}:X".encode()).decode()
        return f"Basic {token}"

    def redacted(self) -> dict:
        k = self.api_key
        return {
            "base_url": self.base_url,
            "api_key": f"{k[:3]}…{k[-2:]}" if len(k) > 6 else "***",
            "agent_name": self.agent_name,
            "agent_email": self.agent_email,
        }

    def __repr__(self) -> str:  # never leak the key through logs / tracebacks
        return f"Credentials({self.redacted()})"


class CredentialStore:
    def __init__(self, path: Path | None = None):
        self.path = path or default_store_path()

    def save(self, creds: Credentials) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, stat.S_IRWXU)
        except OSError:
            pass
        payload = {
            "base_url": creds.base_url,
            "api_key": creds.api_key,
            "agent_name": creds.agent_name,
            "agent_email": creds.agent_email,
        }
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
        os.chmod(self.path, 0o600)

    def load(self) -> Credentials | None:
        if not self.path.exists():
            return None
        data = json.loads(self.path.read_text())
        return Credentials(
            base_url=data["base_url"],
            api_key=data["api_key"],
            agent_name=data.get("agent_name"),
            agent_email=data.get("agent_email"),
        )

    def delete(self) -> bool:
        if self.path.exists():
            self.path.unlink()
            return True
        return False


def resolve_credentials(store: CredentialStore | None = None) -> Credentials:
    """Env vars win (12-factor / container deploys); fall back to the local store."""
    domain, key = os.environ.get(ENV_DOMAIN), os.environ.get(ENV_API_KEY)
    if domain and key:
        return Credentials(base_url=normalize_base_url(domain), api_key=key.strip())
    creds = (store or CredentialStore()).load()
    if creds is None:
        raise ConfigError(
            f"No Freshdesk credentials. Set {ENV_DOMAIN}/{ENV_API_KEY} or run "
            "`freshdesk-connector auth login`."
        )
    return creds
