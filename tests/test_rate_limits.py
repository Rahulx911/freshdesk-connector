"""Rate-limit and resilience behaviour, on virtual time (no real sleeping)."""

import pytest

from freshdesk_connector.errors import RateLimited, UpstreamError


async def test_learns_account_limit_from_headers(make_service):
    svc = make_service(server_limit=400)
    await svc.get_company(501)
    snap = svc.c.rl.snapshot()
    assert snap["account_limit_per_min"] == 400
    assert snap["connector_budget_per_min"] == 320  # keeps 20% for merchant's other apps


async def test_server_429_then_retry_after_success(make_service, clock):
    # The merchant's other integrations already burned the shared account quota,
    # so our very first call gets 429. Client honours Retry-After and succeeds.
    svc = make_service(server_limit=50, external_usage=50, max_wait_s=120)
    assert (await svc.get_company(501))["id"] == 501
    assert clock.sleeps and clock.sleeps[0] >= 55, clock.sleeps
    assert svc.mock_state["requests"] == 2  # one 429 + successful retry


async def test_429_fails_fast_when_wait_exceeds_budget(make_service):
    svc = make_service(server_limit=50, external_usage=50, max_wait_s=5)
    with pytest.raises(RateLimited) as e:
        await svc.get_company(501)
    d = e.value.to_dict()
    assert d["error"] == "rate_limited" and d["retry_after_seconds"] > 5
    assert "Wait about" in d["hint"]


async def test_proactive_throttle_prevents_429(make_service, clock):
    # Connector budget = 10 * (1-0.2) = 8/min; server allows 10. The 9th call
    # should wait client-side instead of ever receiving a 429.
    svc = make_service(server_limit=10, client_limit=10, reserve=0.2, max_wait_s=120)
    for _ in range(9):
        await svc.get_company(501)
    assert svc.mock_state["requests"] == 9   # no 429 round-trip
    assert clock.sleeps and clock.sleeps[0] > 50


async def test_proactive_throttle_fail_fast(make_service):
    svc = make_service(server_limit=10, client_limit=10, reserve=0.2, max_wait_s=5)
    for _ in range(8):
        await svc.get_company(501)
    with pytest.raises(RateLimited):
        await svc.get_company(501)


async def test_retries_transient_5xx(make_service):
    svc = make_service(fail_first_n=2)
    assert (await svc.get_company(501))["id"] == 501
    assert svc.mock_state["requests"] == 3


async def test_gives_up_after_max_retries(make_service):
    svc = make_service(fail_first_n=10)
    with pytest.raises(UpstreamError):
        await svc.get_company(501)
