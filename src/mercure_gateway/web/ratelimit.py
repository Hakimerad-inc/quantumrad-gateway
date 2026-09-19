"""Per-IP rate limiting for the credential-bearing endpoints (review P1-17).

Stdlib only, in-process. The gateway runs a single uvicorn worker, so an
in-process bucket is a real budget and not a per-worker multiple.

The shape deliberately cannot lock an operator out permanently: a fixed window
of allowed failures, then escalating *expiring* lockouts (30 s, 60 s, 120 s …)
that cap at 15 minutes and decay as soon as the attempts stop. Brute-forcing a
password through this is hopeless; mistyping yours three times costs you a
minute, not your appliance.

Keyed on ``request.client.host``. Behind a TLS-terminating proxy every peer
appears as one address, so one attacker can exhaust the budget for everyone
else on that proxy — ``X-Forwarded-For`` is deliberately **not** honoured
unless the deployment opts into trusting proxy headers, the same stance the
bind-security check takes.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from threading import RLock

from fastapi import HTTPException, Request

# A wrong password costs you the rest of the window, then a lockout.
_MAX_FAILURES = 5
_WINDOW_SEC = 60.0

# 30 s, 60 s, 120 s, 240 s … capped.
_BASE_LOCKOUT_SEC = 30.0
_LOCKOUT_GROWTH = 2.0
_MAX_LOCKOUT_SEC = 900.0

_RATE_LIMITED_ATTR = "login_attempt_tracker"


@dataclass
class _AttemptRecord:
    """One client's recent failures and lockout state."""

    failures: list[float] = field(default_factory=list)
    lockout_until: float = 0.0
    consecutive_lockouts: int = 0


@dataclass
class AttemptTracker:
    """In-process failure tracker. One per app (``app.state``), so every
    TestClient gets a fresh bucket and tests do not leak into each other."""

    max_failures: int = _MAX_FAILURES
    window_sec: float = _WINDOW_SEC
    base_lockout_sec: float = _BASE_LOCKOUT_SEC
    max_lockout_sec: float = _MAX_LOCKOUT_SEC
    _lock: RLock = field(default_factory=RLock, repr=False)
    _records: dict[str, _AttemptRecord] = field(default_factory=dict, repr=False)

    def _now(self) -> float:
        # Indirected so tests can move time without patching the stdlib.
        return time.monotonic()

    def _prune(self, record: _AttemptRecord, now: float) -> None:
        record.failures = [t for t in record.failures if now - t < self.window_sec]

    def lockout_remaining(self, key: str) -> float:
        """Seconds left in the current lockout, 0 when none is active."""
        with self._lock:
            record = self._records.get(key)
            if record is None:
                return 0.0
            return max(0.0, record.lockout_until - self._now())

    def record_failure(self, key: str) -> float:
        """Register a failed attempt. Returns the lockout now in force (0 if none).

        A lockout is not imposed on the failure that trips the budget — that
        attempt is already rejected — but on the *next* attempt, which arrives
        from a client that has been told to wait.
        """
        with self._lock:
            now = self._now()
            record = self._records.setdefault(key, _AttemptRecord())
            self._prune(record, now)
            record.failures.append(now)
            if len(record.failures) < self.max_failures:
                return 0.0
            record.consecutive_lockouts += 1
            growth = _LOCKOUT_GROWTH ** (record.consecutive_lockouts - 1)
            lockout = min(self.base_lockout_sec * growth, self.max_lockout_sec)
            record.lockout_until = now + lockout
            # The window is consumed: a fresh start after the lockout expires.
            record.failures = []
            return lockout

    def record_success(self, key: str) -> None:
        """A successful login clears the escalation — one good password ends the
        penalty, it does not carry over to the next mistyped one."""
        with self._lock:
            self._records.pop(key, None)


def _tracker(request: Request) -> AttemptTracker:
    tracker = getattr(request.app.state, _RATE_LIMITED_ATTR, None)
    if tracker is None:
        tracker = AttemptTracker()
        # A test that mounts the router without create_app still gets a bucket.
        setattr(request.app.state, _RATE_LIMITED_ATTR, tracker)
    return tracker


def _client_key(request: Request) -> str:
    client = request.client
    return client.host if client else "unknown"


def enforce_rate_limit(request: Request) -> AttemptTracker:
    """Reject with 429 while a lockout is in force. Call before the credential
    check, and pair with :func:`record_result` on the outcome."""
    tracker = _tracker(request)
    remaining = tracker.lockout_remaining(_client_key(request))
    if remaining > 0:
        raise HTTPException(
            status_code=429,
            detail="too many failed attempts, try again later",
            headers={"Retry-After": str(int(remaining) + 1)},
        )
    return tracker


def record_result(tracker: AttemptTracker, request: Request, *, success: bool) -> None:
    key = _client_key(request)
    if success:
        tracker.record_success(key)
    else:
        tracker.record_failure(key)


def attach_tracker(app: object) -> AttemptTracker:
    """Mint the tracker at app construction (called by ``create_app``)."""
    tracker = AttemptTracker()
    setattr(app.state, _RATE_LIMITED_ATTR, tracker)  # type: ignore[attr-defined]
    return tracker
