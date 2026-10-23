"""The admin token throttle on the real app, and the endpoint that shows how the server sees the caller."""

from __future__ import annotations

import sys
import time

import httpx
import pytest

TEMPLATES = "/admin/api/database/templates"  # cheap, needs only the token
CLIENT_ENDPOINT = "/admin/api/auth/client"


@pytest.fixture
def live(monkeypatch):
    """The settings and the throttle of the *running* app (the bootstrap reloads every kohakuhub module)."""
    config = sys.modules["kohakuhub.config"].cfg.admin
    for name, value in {
        "auth_free_failures": 3,
        "auth_base_wait_seconds": 1,
        "auth_max_wait_seconds": 900,
        "auth_throttle_enabled": True,
        "auth_trusted_proxy_hops": 0,
        "auth_allowlist": [],
        "allow_weak_token": True,
    }.items():
        monkeypatch.setattr(config, name, value)
    module = sys.modules["kohakuhub.auth.admin_token"]
    module.throttle.set_clock(time.time)  # a test may have moved it
    module.throttle.reset()
    return module


def wrong(client, token="not-the-token"):
    return client.get(TEMPLATES, headers={"X-Admin-Token": token})


async def test_wrong_tokens_are_free_three_times_then_the_client_waits(client, admin_client, live):
    for _ in range(3):
        assert (await wrong(client)).status_code == 403
    sixth = await wrong(client)  # the 4th failure is compared (403) and starts the wait
    assert sixth.status_code == 403
    waiting = await wrong(client)
    assert waiting.status_code == 429
    assert waiting.headers["retry-after"] == "1"
    assert waiting.json()["detail"]["retry_after"] == 1


async def test_a_waiting_client_gets_429_even_with_the_right_token_then_recovers(client, admin_client, live):
    for _ in range(4):
        await wrong(client)
    right = {"X-Admin-Token": admin_client.headers["X-Admin-Token"]}
    assert (await client.get(TEMPLATES, headers=right)).status_code == 429
    live.throttle.set_clock(lambda: time.time() + 5)  # the wait is over
    assert (await client.get(TEMPLATES, headers=right)).status_code == 200


async def test_requests_without_a_token_are_not_counted(client, live):
    for _ in range(10):
        assert (await client.get(TEMPLATES)).status_code == 401
    assert (await wrong(client)).status_code == 403


async def test_a_forged_forwarded_header_does_not_buy_a_fresh_count(client, live, monkeypatch):
    monkeypatch.setattr(sys.modules["kohakuhub.config"].cfg.admin, "auth_trusted_proxy_hops", 1)
    headers = lambda forged: {"X-Admin-Token": "nope", "X-Forwarded-For": f"{forged}, 198.51.100.77"}
    for index in range(4):
        await client.get(TEMPLATES, headers=headers(f"10.0.0.{index}"))
    blocked = await client.get(TEMPLATES, headers=headers("10.9.9.9"))
    assert blocked.status_code == 429


async def test_the_client_endpoint_shows_what_the_server_computed(admin_client, live, monkeypatch):
    response = await admin_client.get(CLIENT_ENDPOINT)
    assert response.status_code == 200
    body = response.json()
    assert body["hops"] == 0 and body["forwarded_for_seen"] is False and body["trusted"] is True
    assert body["client"] and body["address"]
    assert body["forwarded_for"] == [] and body["peer"]  # nothing forwarded: the peer is the address

    monkeypatch.setattr(sys.modules["kohakuhub.config"].cfg.admin, "auth_trusted_proxy_hops", 1)
    forwarded = await admin_client.get(CLIENT_ENDPOINT, headers={"X-Forwarded-For": "forged, 203.0.113.9"})
    assert forwarded.json()["client"] == "203.0.113.9"
    assert forwarded.json()["forwarded_for_seen"] is True and forwarded.json()["hops"] == 1
    # What the proxies wrote, left to right, so the operator can count from the right to the address that is theirs
    assert forwarded.json()["forwarded_for"] == ["forged", "203.0.113.9"]
    assert forwarded.json()["peer"] == body["peer"]


async def test_the_client_endpoint_lists_every_forwarded_header_line_in_order(admin_client, live):
    response = await admin_client.get(
        CLIENT_ENDPOINT,
        headers=[("X-Forwarded-For", "198.51.100.7, 203.0.113.9"), ("X-Forwarded-For", " 192.0.2.1 ,, 2001:db8::5")],
    )

    body = response.json()
    assert body["forwarded_for"] == ["198.51.100.7", "203.0.113.9", "192.0.2.1", "2001:db8::5"]
    assert body["forwarded_for_seen"] is True and body["trusted"] is False  # hops 0: not believed


async def test_the_client_endpoint_needs_the_token(client, live):
    assert (await client.get(CLIENT_ENDPOINT)).status_code == 401
    assert (await wrong(client)).status_code == 403
    assert (await client.get(CLIENT_ENDPOINT, headers={"X-Admin-Token": "nope"})).status_code == 403


async def test_a_weak_token_closes_the_admin_api_with_a_clear_answer(admin_client, live, monkeypatch):
    config = sys.modules["kohakuhub.config"].cfg.admin
    monkeypatch.setattr(config, "allow_weak_token", False)  # the test token is only 16 characters
    response = await admin_client.get(TEMPLATES)
    assert response.status_code == 503
    assert "too weak" in response.json()["detail"]["error"]


async def test_the_repository_routes_that_accept_the_token_are_throttled_too(client, admin_client, live):
    # /api/repos/delete takes either a user or the admin token (get_current_user_or_admin).
    body = {"type": "model", "name": "nobody/nothing"}
    for _ in range(4):
        await client.request("DELETE", "/api/repos/delete", json=body, headers={"X-Admin-Token": "nope"})
    blocked = await client.request("DELETE", "/api/repos/delete", json=body, headers={"X-Admin-Token": "nope"})
    assert blocked.status_code == 429


async def test_the_wait_is_in_the_database_so_another_worker_sees_it(client, live):
    from kohakuhub.auth.admin_throttle import SharedAdminAuthThrottle

    for _ in range(4):
        await wrong(client)  # on this worker: three free failures, the fourth starts a wait

    other_worker = SharedAdminAuthThrottle()  # another process: empty memory, same table
    assert other_worker.check("127.0.0.1") == 1
    assert other_worker.check("203.0.113.9") == 0


async def test_the_throttle_still_works_when_the_table_is_gone(client, live, monkeypatch):
    models = sys.modules["kohakuhub.db"]
    monkeypatch.setattr(models.AdminAuthClient, "get_or_none", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no table")))
    monkeypatch.setattr(models.AdminAuthClient, "select", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no table")))

    statuses = [(await wrong(client)).status_code for _ in range(6)]

    assert statuses[:4] == [403, 403, 403, 403]  # never a 500 because the table is down
    assert statuses[4:] == [429, 429]  # and the wait is kept in memory

