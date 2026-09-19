"""Login rate limiting (review P1-17).

The limiter must make brute-forcing the admin password hopeless without ever
locking an operator out permanently: a budget of failures, then escalating
*expiring* lockouts that decay the moment the attempts stop.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mercure_gateway.config import default_config
from mercure_gateway.spool import Spool
from mercure_gateway.spool.db import mem_database
from mercure_gateway.web import create_app
from mercure_gateway.web.auth import hash_password
from mercure_gateway.web.ratelimit import AttemptTracker


@pytest.fixture()
def authed_client() -> TestClient:
    cfg = default_config()
    cfg.web_ui.auth_enabled = True
    cfg.web_ui.auth_password_hash = hash_password("s3cret")
    return TestClient(create_app(cfg, Spool(mem_database())))


def _wrong(c: TestClient) -> int:
    return c.post("/api/login", json={"password": "nope"}).status_code


def test_wrong_password_is_rejected(authed_client: TestClient) -> None:
    assert _wrong(authed_client) == 401


def test_failures_within_budget_do_not_lock_out(authed_client: TestClient) -> None:
    """Four wrong passwords cost nothing — the budget exists to be spent."""
    for _ in range(4):
        assert _wrong(authed_client) == 401
    # The fifth wrong password is still a plain 401, not a lockout signal.
    assert _wrong(authed_client) == 401


def test_the_next_attempt_after_the_budget_is_429(authed_client: TestClient) -> None:
    for _ in range(5):
        assert _wrong(authed_client) == 401
    r = authed_client.post("/api/login", json={"password": "nope"})
    assert r.status_code == 429
    assert "Retry-After" in r.headers


def test_a_lockout_is_a_lockout_even_for_the_right_password(
    authed_client: TestClient,
) -> None:
    """While a lockout is in force every attempt is refused, including a correct
    one — the lockout would be advisory otherwise. It is short (30 s) and
    expiring, which is what makes this survivable for a mistyping operator."""
    for _ in range(5):
        _wrong(authed_client)
    r = authed_client.post("/api/login", json={"password": "s3cret"})
    assert r.status_code == 429
    assert "Retry-After" in r.headers
    assert authed_client.get("/api/system/status").status_code == 401


def test_a_successful_login_clears_the_escalation(authed_client: TestClient) -> None:
    for _ in range(4):
        _wrong(authed_client)
    assert authed_client.post("/api/login", json={"password": "s3cret"}).status_code == 200
    # The penalty does not carry over to the next mistyped password.
    for _ in range(4):
        assert _wrong(authed_client) == 401


def test_lockouts_escalate_and_expire(tracker: AttemptTracker) -> None:
    """Each full window of 5 failures escalates: 30 s, 60 s, 120 s … never past
    the 15-minute cap, and every lockout expires."""
    # The first window trips at the 5th failure.
    for _ in range(4):
        assert tracker.record_failure("a") == 0.0
    assert tracker.record_failure("a") == 30.0  # trips
    assert 29.0 < tracker.lockout_remaining("a") <= 30.0

    # Each subsequent window doubles the penalty.
    for expected in (60.0, 120.0, 240.0, 480.0, 900.0, 900.0):
        for _ in range(4):
            tracker.record_failure("a")
        assert tracker.record_failure("a") == expected
        assert tracker.lockout_remaining("a") > 0.0


def test_lockouts_decay_when_the_attempts_stop(tracker: AttemptTracker) -> None:
    """The escalation is per streak, not per lifetime of the client address."""
    for _ in range(5):
        tracker.record_failure("a")
    assert tracker.lockout_remaining("a") > 0
    tracker.record_success("a")
    assert tracker.lockout_remaining("a") == 0.0
    # A clean slate: the next streak starts at the base lockout again.
    for _ in range(4):
        assert tracker.record_failure("a") == 0.0
    assert tracker.record_failure("a") == 30.0


def test_clients_are_tracked_separately(tracker: AttemptTracker) -> None:
    """One attacker does not burn the budget for every other operator."""
    for _ in range(5):
        tracker.record_failure("attacker")
    assert tracker.lockout_remaining("attacker") > 0
    assert tracker.lockout_remaining("operator") == 0.0


def test_failures_outside_the_window_do_not_count(tracker: AttemptTracker) -> None:
    """A slow trickle of one wrong password per minute never locks anyone."""
    now = tracker._now()
    try:
        tracker._now = lambda: now  # type: ignore[method-assign]
        for i in range(10):
            tracker._now = lambda i=i: now + i * 70.0  # type: ignore[method-assign]
            assert tracker.record_failure("a") == 0.0
    finally:
        tracker._now = lambda: now  # type: ignore[method-assign]
    assert tracker.lockout_remaining("a") == 0.0


@pytest.fixture()
def tracker() -> AttemptTracker:
    return AttemptTracker()
