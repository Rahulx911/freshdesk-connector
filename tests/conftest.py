from __future__ import annotations

import httpx
import pytest

from mock_server import data as mock_data
from mock_server.app import app as mock_app
from woocommerce_connector.auth import Credentials
from woocommerce_connector.client import WooClient
from woocommerce_connector.ratelimit import RateLimiter
from woocommerce_connector.service import WooService

STORE_URL = "https://shop.example.com"

# Every environment variable the connectors read. Cleared for every test so a
# developer who has exported real credentials to run the live demo does not
# get different results from someone who has not. This bit us: a test that
# writes credentials to disk and reads them back passed on a clean shell and
# failed on one with WOO_CONSUMER_SECRET exported, because load() checks the
# environment first by design.
CONNECTOR_ENV = (
    "WOO_STORE_URL", "WOO_CONSUMER_KEY", "WOO_CONSUMER_SECRET",
    "WOO_CONNECTOR_HOME", "WOO_TENANTS_FILE", "WOO_INSECURE_HTTP_HOSTS",
    "RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET", "RAZORPAY_BASE_URL",
    "RAZORPAY_CONNECTOR_HOME",
    "LOG_LEVEL", "LOG_FORMAT", "AUDIT_LOG", "EVAL_MODEL",
)


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    """Run every test against a known-empty environment.

    A test suite whose result depends on the shell it is run from is not a
    test suite. Tests that need one of these set it themselves.
    """
    for name in CONNECTOR_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def creds() -> Credentials:
    return Credentials(STORE_URL, mock_data.CONSUMER_KEY, mock_data.CONSUMER_SECRET)


@pytest.fixture
def transport() -> httpx.ASGITransport:
    return httpx.ASGITransport(app=mock_app)


@pytest.fixture
async def client(creds, transport):
    c = WooClient(creds, transport=transport, rate_limiter=RateLimiter(10_000), max_wait_s=2.0)
    try:
        yield c
    finally:
        await c.aclose()


@pytest.fixture
async def service(client) -> WooService:
    return WooService(client)


@pytest.fixture
async def open_service(creds, transport):
    """Service with PII redaction off, for tests that assert raw values."""
    c = WooClient(creds, transport=transport, rate_limiter=RateLimiter(10_000), max_wait_s=2.0)
    try:
        yield WooService(c, redact_pii=False)
    finally:
        await c.aclose()
