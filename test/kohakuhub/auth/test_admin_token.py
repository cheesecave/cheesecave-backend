"""The admin token check with its throttle, weak-token guard and client rules (one function for every entry point)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import kohakuhub.auth.admin_token as admin_token
from kohakuhub.auth.admin_throttle import AdminAuthThrottle

TOKEN = "t" * 40


class Headers:
    def __init__(self, **lines):
        self._lines = {k.replace("_", "-").lower(): v for k, v in lines.items()}

    def getlist(self, name):
        return list(self._lines.get(name.lower(), []))


def request(peer="198.51.100.20", **forwarded):
    return SimpleNamespace(
        client=SimpleNamespace(host=peer), headers=Headers(**forwarded), state=SimpleNamespace(), cookies={}, method="GET"
    )


NOW = {"t": 5000.0}


@pytest.fixture(autouse=True)
def admin_settings(monkeypatch):
    """The settings, and a throttle that counts in memory (what the shared table does is tested apart)."""
    NOW["t"] = 5000.0
    admin = admin_token.cfg.admin
    for name, value in {
        "enabled": True,
        "secret_token": TOKEN,
        "allow_weak_token": False,
        "min_token_length": 32,
        "auth_throttle_enabled": True,
        "auth_free_failures": 5,
        "auth_base_wait_seconds": 1,
        "auth_max_wait_seconds": 900,
        "auth_reset_seconds": 1800,
        "auth_alert_per_minute": 50,
        "auth_trusted_proxy_hops": 0,
        "auth_allowlist": [],
    }.items():
        monkeypatch.setattr(admin, name, value)
    monkeypatch.setattr(admin_token, "throttle", AdminAuthThrottle(clock=lambda: NOW["t"]))
    yield


@pytest.fixture
def clock():
    return NOW


def outcome(req, token):
    return admin_token.check_admin_token(req, token).outcome


def test_disabled_and_missing_come_before_anything_else(monkeypatch):
    monkeypatch.setattr(admin_token.cfg.admin, "enabled", False)
    assert outcome(request(), TOKEN) == "disabled"
    monkeypatch.setattr(admin_token.cfg.admin, "enabled", True)
    assert outcome(request(), None) == "missing"
    assert outcome(request(), "") == "missing"


def test_a_valid_token_is_ok_and_a_wrong_one_is_invalid():
    assert outcome(request(), TOKEN) == "ok"
    assert outcome(request(), "wrong") == "invalid"


@pytest.mark.parametrize("token", ["change-me-in-production", "short", "x" * 31])
def test_a_weak_token_disables_the_admin_api(monkeypatch, token):
    monkeypatch.setattr(admin_token.cfg.admin, "secret_token", token)
    # Even the right (weak) token is refused, and so is a request that sends nothing.
    assert outcome(request(), token) == "weak"
    assert outcome(request(), None) == "weak"


def test_the_weak_token_override_is_for_development_and_tests(monkeypatch):
    monkeypatch.setattr(admin_token.cfg.admin, "secret_token", "dev-token")
    monkeypatch.setattr(admin_token.cfg.admin, "allow_weak_token", True)
    assert outcome(request(), "dev-token") == "ok"


def test_the_weak_token_is_reported_once_in_the_log(monkeypatch):
    monkeypatch.setattr(admin_token.cfg.admin, "secret_token", "short")
    monkeypatch.setattr(admin_token, "_weak_reported", False)
    outcome(request(), "short")
    outcome(request(), "short")
    assert admin_token._weak_reported is True


def test_five_wrong_tokens_are_free_then_the_client_waits(clock):
    req = request()
    assert [outcome(req, "wrong") for _ in range(5)] == ["invalid"] * 5
    assert outcome(req, "wrong") == "invalid"  # the 6th is still compared, and starts a 1 s wait
    blocked = admin_token.check_admin_token(req, "wrong")
    assert blocked.outcome == "blocked" and blocked.retry_after == 1


def test_a_waiting_client_is_refused_even_with_the_right_token_and_is_not_counted(clock):
    req = request()
    for _ in range(6):
        outcome(req, "wrong")
    for _ in range(20):  # asking again during the wait neither counts nor extends it
        assert admin_token.check_admin_token(req, TOKEN).outcome == "blocked"
    clock["t"] += 1.1
    assert outcome(req, TOKEN) == "ok"


def test_the_wait_doubles_with_each_failure_after_the_wait(clock):
    req = request()
    waits = []
    for _ in range(9):
        result = admin_token.check_admin_token(req, "wrong")
        assert result.outcome == "invalid"
        waits.append(admin_token.throttle.check("198.51.100.20"))
        clock["t"] += max(waits[-1], 0) + 0.01
    assert waits == [0, 0, 0, 0, 0, 1, 2, 4, 8]


def test_a_valid_token_resets_the_clients_count(clock):
    req = request()
    for _ in range(4):
        outcome(req, "wrong")
    assert outcome(req, TOKEN) == "ok"
    assert [outcome(req, "wrong") for _ in range(5)] == ["invalid"] * 5
    assert admin_token.throttle.check("198.51.100.20") == 0


def test_another_client_is_not_affected(clock):
    attacker, administrator = request(peer="198.51.100.20"), request(peer="203.0.113.5")
    for _ in range(8):
        outcome(attacker, "wrong")
    assert outcome(attacker, TOKEN) == "blocked"
    assert outcome(administrator, TOKEN) == "ok"


def test_the_allowlist_is_never_counted_or_delayed(monkeypatch, clock):
    monkeypatch.setattr(admin_token.cfg.admin, "auth_allowlist", ["198.51.100.0/24"])
    req = request()
    assert [outcome(req, "wrong") for _ in range(30)] == ["invalid"] * 30
    assert outcome(req, TOKEN) == "ok"


def test_the_throttle_can_be_turned_off(monkeypatch, clock):
    monkeypatch.setattr(admin_token.cfg.admin, "auth_throttle_enabled", False)
    assert [outcome(request(), "wrong") for _ in range(30)] == ["invalid"] * 30


def test_behind_an_untrusted_proxy_nobody_waits_because_everybody_is_the_same_client(monkeypatch, clock):
    monkeypatch.setattr(admin_token, "_warned_shared_client", False)
    req = request(peer="172.18.0.5", x_forwarded_for=["203.0.113.9"])  # a proxy header, but hops = 0
    assert [outcome(req, "wrong") for _ in range(30)] == ["invalid"] * 30
    assert outcome(req, TOKEN) == "ok"
    assert admin_token._warned_shared_client is True


def test_with_one_trusted_proxy_clients_are_told_apart_by_the_forwarded_address(monkeypatch, clock):
    monkeypatch.setattr(admin_token.cfg.admin, "auth_trusted_proxy_hops", 1)
    one = request(peer="172.18.0.5", x_forwarded_for=["forged, 198.51.100.20"])
    other = request(peer="172.18.0.5", x_forwarded_for=["forged, 203.0.113.5"])
    for _ in range(8):
        outcome(one, "wrong")
    assert outcome(one, TOKEN) == "blocked"
    assert outcome(other, TOKEN) == "ok"
    # Changing the forged left part does not give the attacker a fresh budget.
    assert outcome(request(peer="172.18.0.5", x_forwarded_for=["other forgery, 198.51.100.20"]), TOKEN) == "blocked"


def test_failures_are_logged_without_the_token(monkeypatch, caplog):
    seen = []
    monkeypatch.setattr(admin_token.logger, "warning", lambda message, *a, **k: seen.append(str(message)))
    req = request()
    req.headers = Headers(user_agent=["curl/8 " + "x" * 200])
    outcome(req, "super-secret-guess")
    assert seen and all("super-secret-guess" not in line for line in seen)
    assert "198.51.100.20" in seen[0] and "failure 1" in seen[0]


def test_a_high_failure_rate_raises_one_alert(monkeypatch, clock):
    monkeypatch.setattr(admin_token.cfg.admin, "auth_alert_per_minute", 3)
    monkeypatch.setattr(admin_token.cfg.admin, "auth_allowlist", ["0.0.0.0/0"])  # counting off, alert still on
    warnings = []
    monkeypatch.setattr(admin_token.logger, "warning", lambda message, *a, **k: warnings.append(str(message)))
    for _ in range(10):
        outcome(request(), "wrong")
    alerts = [line for line in warnings if "failed admin token attempts" in line]
    assert len(alerts) == 1


def test_the_start_of_a_wait_is_logged(monkeypatch, clock):
    info = []
    monkeypatch.setattr(admin_token.logger, "info", lambda message, *a, **k: info.append(str(message)))
    for _ in range(6):
        outcome(request(), "wrong")
    assert any("must wait 1s" in line for line in info)


def test_verify_admin_token_maps_every_outcome_to_http(monkeypatch, clock):
    import asyncio

    def call(req, token):
        return asyncio.run(admin_token.verify_admin_token(req, token))

    assert call(request(), TOKEN) is True
    with pytest.raises(HTTPException) as missing:
        call(request(), None)
    assert missing.value.status_code == 401
    with pytest.raises(HTTPException) as invalid:
        call(request(), "wrong")
    assert invalid.value.status_code == 403 and invalid.value.detail == {"error": "Invalid admin token"}

    for _ in range(5):
        with pytest.raises(HTTPException):
            call(request(), "wrong")
    with pytest.raises(HTTPException) as waiting:
        call(request(), TOKEN)
    assert waiting.value.status_code == 429
    assert waiting.value.headers["Retry-After"] == "1"
    assert waiting.value.detail["retry_after"] == 1

    monkeypatch.setattr(admin_token.cfg.admin, "secret_token", "weak")
    with pytest.raises(HTTPException) as weak:
        call(request(), "weak")
    assert weak.value.status_code == 503 and "too weak" in weak.value.detail["error"]

    monkeypatch.setattr(admin_token.cfg.admin, "enabled", False)
    with pytest.raises(HTTPException) as disabled:
        call(request(), TOKEN)
    assert disabled.value.status_code == 503 and "disabled" in disabled.value.detail["error"]


def test_threads_arriving_together_get_no_more_guesses_than_one_at_a_time(monkeypatch, clock):
    """The look at the wait, the comparison and the record are one decision, so a burst cannot all slip in."""
    import threading
    import time

    compared = []
    real = admin_token._matches

    def slow_compare(token):
        compared.append(token)
        time.sleep(0.02)  # long enough for every thread to have arrived
        return real(token)

    monkeypatch.setattr(admin_token, "_matches", slow_compare)
    results = []

    def guess(index):
        results.append(outcome(request(), f"wrong-{index}"))

    threads = [threading.Thread(target=guess, args=(i,)) for i in range(25)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(compared) == 6  # five free guesses and the sixth, which starts the wait
    assert results.count("invalid") == 6 and results.count("blocked") == 19


def test_the_wait_between_the_limits_is_never_shorter_than_its_start(monkeypatch, clock):
    monkeypatch.setattr(admin_token.cfg.admin, "auth_base_wait_seconds", 30)
    monkeypatch.setattr(admin_token.cfg.admin, "auth_max_wait_seconds", 10)  # set the wrong way round

    params = admin_token._params()

    assert params.base_wait == 30 and params.max_wait == 30

