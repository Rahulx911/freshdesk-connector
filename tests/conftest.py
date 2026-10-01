from __future__ import annotations

import httpx
import pytest

from freshdesk_connector.auth import Credentials
from freshdesk_connector.client import FreshdeskClient, RateLimiter
from freshdesk_connector.normalize import Normalizer
from freshdesk_connector.service import FreshdeskService
from mock_server.app import MOCK_API_KEY, create_app


class FakeClock:
    """Shared virtual time for mock server + client so rate-limit tests run instantly."""

    def __init__(self):
        self.t = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def make_service(clock):
    created = []

    def _make(*, api_key=MOCK_API_KEY, server_limit=100, fail_first_n=0, client_limit=None,
              reserve=0.2, max_wait_s=20.0, redact=False, external_usage=0,
              private_notes=False):
        app = create_app(rate_limit_per_min=server_limit, fail_first_n=fail_first_n,
                         external_usage=external_usage, clock=clock)
        creds = Credentials(base_url="http://testserver", api_key=api_key)
        rl = RateLimiter(limit_per_min=client_limit or 50, reserve_fraction=reserve, clock=clock)
        client = FreshdeskClient(creds, rate_limiter=rl, max_wait_s=max_wait_s,
                                 transport=httpx.ASGITransport(app=app), sleep=clock.sleep)
        svc = FreshdeskService(client, Normalizer(redact_pii=redact, include_private_notes=private_notes))
        svc.mock_state = app.state.mock
        created.append(client)
        return svc

    yield _make
