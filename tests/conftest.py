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
