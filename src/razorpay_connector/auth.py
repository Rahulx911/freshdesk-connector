"""Razorpay API credentials.

Razorpay authenticates with a key id and key secret over HTTP Basic, and the
key id carries its own environment marker: `rzp_test_` or `rzp_live_`. We read
that and surface it, because an agent answering a customer from test-mode data
is a worse failure than an agent that cannot answer at all.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from woocommerce_connector.errors import ConfigError

ENV_KEY_ID = "RAZORPAY_KEY_ID"
ENV_KEY_SECRET = "RAZORPAY_KEY_SECRET"  # noqa: S105  # nosec B105
ENV_BASE_URL = "RAZORPAY_BASE_URL"      # overridden only to point at the mock

DEFAULT_BASE_URL = "https://api.razorpay.com"


def config_home() -> Path:
    base = os.environ.get("RAZORPAY_CONNECTOR_HOME") or os.path.join(
        os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "razorpay-connector"
    )
    return Path(base)


def _creds_path() -> Path:
    return config_home() / "credentials.json"


def mask_key(key: str) -> str:
    if not key:
        return ""
    return f"{key[:12]}…" if len(key) > 14 else key[:6] + "…"


@dataclass(frozen=True)
class Credentials:
    key_id: str
    key_secret: str
    base_url: str = DEFAULT_BASE_URL

    @property
    def mode(self) -> str:
        """test / live / unknown, read from the key id itself."""
        if self.key_id.startswith("rzp_test_"):
            return "test"
        if self.key_id.startswith("rzp_live_"):
            return "live"
        return "unknown"

    def api_base(self) -> str:
        return self.base_url.rstrip("/") + "/v1"

    def redacted(self) -> dict:
        return {"key_id": mask_key(self.key_id), "mode": self.mode, "base_url": self.base_url}


def save(creds: Credentials) -> Path:
    home = config_home()
    home.mkdir(parents=True, exist_ok=True)
    os.chmod(home, stat.S_IRWXU)
    path = _creds_path()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w") as fh:
        json.dump({"key_id": creds.key_id, "key_secret": creds.key_secret,
                   "base_url": creds.base_url}, fh)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return path


def load() -> Credentials:
    key_id = os.environ.get(ENV_KEY_ID)
    key_secret = os.environ.get(ENV_KEY_SECRET)
    base = os.environ.get(ENV_BASE_URL, DEFAULT_BASE_URL)
    if key_id and key_secret:
        return Credentials(key_id, key_secret, base)

    path = _creds_path()
    if not path.exists():
        raise ConfigError(
            f"No Razorpay credentials. Set {ENV_KEY_ID} and {ENV_KEY_SECRET}."
        )
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise ConfigError(f"Could not read {path}: {e}") from e
    if not data.get("key_id") or not data.get("key_secret"):
        raise ConfigError("Stored Razorpay credentials are incomplete")
    return Credentials(data["key_id"], data["key_secret"],
                       data.get("base_url", DEFAULT_BASE_URL))
