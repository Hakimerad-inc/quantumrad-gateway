"""Hub-signed audit head anchors (review M4 remainder).

The anchor file gives *integrity*, not *authenticity*: anyone who can rewrite
the spool DB can also rewrite ``audit-heads.txt``.  The fix (per decision) is a
**hub-held signing service**: the gateway POSTs each chain head to the
bookkeeper's ``POST /anchor`` endpoint, the hub signs the head with its Ed25519
private key, and the gateway stores ``{head, ts, signature}`` in a local JSONL
file.  The gateway never holds a signing key; :func:`verify_anchor_signatures`
re-verifies every stored signature offline against the hub's public key.

That check only means something if something *runs* it: it had no production
caller at all, so a tampered chain was undetectable unless an operator thought
to ask.  :class:`AnchorVerifier` puts it on a timer instead (review P0-10).

Isolation (US-10): like :class:`~mercure_gateway.hub_events.HubEventStreamer`,
posting never blocks the caller — the file anchor is written synchronously
(cheap, local) and the network round-trip happens on a bounded daemon queue
with requeue-at-head + exponential backoff.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PublicKey,
)

from mercure_gateway.audit import anchor_head_to_file
from mercure_gateway.update import b64decode_strict, load_ed25519_public_key

__all__ = [
    "AnchorError",
    "AnchorVerification",
    "AnchorVerifier",
    "SignedHeadAnchorer",
    "verify_anchor_signatures",
]

logger = logging.getLogger(__name__)

_BASE_BACKOFF_SEC = 0.2
_MAX_BACKOFF_SEC = 30.0
_DEFAULT_MAX_QUEUE = 200
_DEFAULT_TIMEOUT_SEC = 10.0
# Anchor verification cadence (review P0-10): often enough that a tampered
# audit log surfaces between scrape intervals, rarely enough that a large
# anchor file is not read constantly. The first pass is deferred so a boot
# with a cold cache is not charged for it.
_DEFAULT_VERIFY_INTERVAL_SEC = 300.0
_DEFAULT_VERIFY_INITIAL_DELAY_SEC = 5.0

# Post function contract: mirrors requests.post(url, json=..., headers=...,
# timeout=...). Injectable so tests can supply a hand-written fake.
PostFn = Callable[..., Any]


@dataclass(frozen=True)
class AnchorError:
    """One unverifiable line in a signed-anchor file."""

    line_no: int
    reason: str


class SignedHeadAnchorer:
    """File-anchor + hub-sign each audit chain head, never blocking appends.

    ``anchor(head)`` appends the head to *file_anchor_path* synchronously
    (preserving the review-M4 file-anchor behaviour exactly) and enqueues a
    sign request for the background worker.  The worker POSTs to
    ``{bookkeeper_url}/anchor`` with Token auth (TD-19); a 2xx reply carrying
    ``{"signature": "<base64 raw Ed25519 over the head hex string>"}`` is
    appended to *signed_anchor_path* as one JSON object per line.  Failures
    requeue at the head with exponential backoff; once retries are exhausted
    the head stays file-anchored only (integrity, not authenticity) and the
    condition is logged.
    """

    def __init__(
        self,
        bookkeeper_url: str,
        api_key: str,
        file_anchor_path: Path,
        signed_anchor_path: Path,
        *,
        verify_key: str | bytes | Ed25519PublicKey | None = None,
        post: PostFn | None = None,
        gateway_name: str = "mercure-gateway",
        max_queue_size: int = _DEFAULT_MAX_QUEUE,
        timeout_sec: float = _DEFAULT_TIMEOUT_SEC,
        max_retries: int = 5,
    ) -> None:
        self._url = bookkeeper_url.rstrip("/") + "/anchor"
        self._api_key = api_key
        self._file_anchor = anchor_head_to_file(file_anchor_path)
        self._signed_path = Path(signed_anchor_path)
        self._post: PostFn = post if post is not None else requests.post
        self._gateway_name = gateway_name
        self._timeout = timeout_sec
        self._max_retries = max_retries
        self._queue: deque[str] = deque(maxlen=max_queue_size)
        self._lock = threading.Lock()
        self._wakeup = threading.Event()
        self._stop_event = threading.Event()
        self._worker: threading.Thread | None = None
        # Verification is optional at construction; the anchorer never needs
        # the key itself, but holding it lets flush() self-check in tests and
        # documents the trust anchor for the composition root.
        self._verify_key = verify_key

    # ── lifecycle ────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._worker is not None:
            return
        self._stop_event.clear()
        self._worker = threading.Thread(
            target=self._work, name="audit-anchor-signer", daemon=True
        )
        self._worker.start()

    def set_gateway_name(self, name: str) -> None:
        self._gateway_name = name

    # ── anchor path (called from AuditLog.append) ───────────────────────

    def anchor(self, head: str) -> None:
        """File-anchor synchronously; enqueue the hub-sign request."""
        self._file_anchor(head)
        with self._lock:
            if self._queue.maxlen is not None and len(self._queue) == self._queue.maxlen:
                logger.debug("anchor queue full — dropping oldest head")
            self._queue.append(head)  # deque(maxlen) drops oldest automatically
        self._wakeup.set()

    def flush(self, timeout: float = 30.0) -> None:
        """Wait until the queue drains or *timeout* elapses."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if not self._queue:
                    return
            time.sleep(0.02)
        logger.warning("anchor flush timed out with %d heads pending", len(self._queue))

    def stop(self) -> None:
        """Signal the worker to drain remaining heads and exit."""
        self._stop_event.set()
        self._wakeup.set()
        if self._worker is not None:
            self._worker.join(timeout=30.0)
            self._worker = None

    # ── worker ───────────────────────────────────────────────────────────

    def _work(self) -> None:
        backoff = _BASE_BACKOFF_SEC
        retries = 0
        while True:
            # Drain the whole queue per wakeup: anchor() sets _wakeup once per
            # append, so with bursts pending the event is usually already
            # clear by the time the worker loops — wait() only as long as the
            # queue is empty.
            with self._lock:
                empty = not self._queue
            if empty:
                self._wakeup.wait(timeout=_MAX_BACKOFF_SEC)
                self._wakeup.clear()
            if self._stop_event.is_set() and self._empty():
                return
            head = self._peek()
            if head is None:
                if self._stop_event.is_set():
                    return
                backoff = _BASE_BACKOFF_SEC
                continue
            if self._sign(head):
                with self._lock:
                    self._queue.popleft()
                retries = 0
                backoff = _BASE_BACKOFF_SEC
            else:
                retries += 1
                if retries >= self._max_retries:
                    logger.warning(
                        "hub anchor signing failed after %d retries for head %.12s… "
                        "(head remains file-anchored only)",
                        retries,
                        head,
                    )
                    with self._lock:
                        self._queue.popleft()
                    retries = 0
                    backoff = _BASE_BACKOFF_SEC
                else:
                    self._stop_event.wait(backoff)
                    backoff = min(backoff * 2, _MAX_BACKOFF_SEC)

    def _empty(self) -> bool:
        with self._lock:
            return not self._queue

    def _peek(self) -> str | None:
        with self._lock:
            return self._queue[0] if self._queue else None

    def _sign(self, head: str) -> bool:
        """POST one head for signing; append the JSONL entry on success."""
        try:
            resp = self._post(
                self._url,
                json={
                    "gateway": self._gateway_name,
                    "head": head,
                    "ts": datetime.now(UTC).isoformat(),
                },
                headers={"Authorization": f"Token {self._api_key}"},  # TD-19
                timeout=self._timeout,
            )
        except Exception:  # noqa: BLE001 — US-10: hub failure must not propagate
            logger.debug("hub anchor request failed for head %.12s…", head, exc_info=True)
            return False
        status = getattr(resp, "status_code", getattr(resp, "status", 0))
        if not (200 <= int(status) < 300):
            return False
        try:
            payload = resp.json()
            signature = str(payload["signature"])
        except Exception:  # noqa: BLE001 — malformed reply = not signed
            logger.debug("hub anchor reply malformed for head %.12s…", head)
            return False
        entry = json.dumps(
            {"head": head, "ts": datetime.now(UTC).isoformat(), "signature": signature},
            separators=(",", ":"),
        )
        try:
            self._signed_path.parent.mkdir(parents=True, exist_ok=True)
            with self._signed_path.open("a", encoding="utf-8") as fh:
                fh.write(entry + "\n")
                fh.flush()
        except OSError:
            logger.exception("cannot write signed anchor file %s", self._signed_path)
            return False
        return True


def verify_anchor_signatures(
    path: Path, public_key: str | bytes | Ed25519PublicKey
) -> tuple[bool, list[AnchorError]]:
    """Offline verification of a signed-anchor JSONL file (or plain head file).

    A *plain* anchor file (one 64-hex hash per line, no JSON) verifies
    trivially — it exists and is well-formed.  For JSONL entries each
    ``signature`` must verify, with the hub's Ed25519 public key, over the
    exact head string's UTF-8 bytes.  Malformed lines are reported, never
    raised.
    """
    key = load_ed25519_public_key(public_key)
    errors: list[AnchorError] = []
    if key is None:
        return False, [AnchorError(0, "no verification key configured")]
    text = Path(path).read_text(encoding="utf-8")
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        if not line.startswith("{"):
            # Plain head-anchor file format.
            if len(line) != 64:
                errors.append(AnchorError(line_no, "malformed head (expected 64 hex chars)"))
            continue
        try:
            entry = json.loads(line)
            head = str(entry["head"])
            signature = str(entry["signature"])
        except (json.JSONDecodeError, KeyError, TypeError):
            errors.append(AnchorError(line_no, "malformed anchor entry"))
            continue
        raw_sig = b64decode_strict(signature)
        if raw_sig is None or len(raw_sig) != 64:
            errors.append(AnchorError(line_no, "signature is not valid base64 Ed25519"))
            continue
        try:
            key.verify(raw_sig, head.encode("utf-8"))
        except Exception:  # noqa: BLE001 — any verify failure is a report line
            errors.append(AnchorError(line_no, "signature does not verify"))
    return not errors, errors


@dataclass(frozen=True)
class AnchorVerification:
    """One scheduled pass of :func:`verify_anchor_signatures` (review P0-10)."""

    ok: bool
    errors: tuple[AnchorError, ...]
    # Signature verification is a *read*: an absent file is a gateway that has
    # appended no chain head yet, not a tampered anchor.
    file_absent: bool = False


class AnchorVerifier:
    """Re-verify stored hub signatures on a timer (review P0-10).

    ``verify_anchor_signatures`` had no production caller at all — the audit
    chain's authenticity was true only when an operator thought to run the
    CLI script or hit ``/api/audit/verify`` (which replays the *internal* chain
    hashes and so cannot detect a whole-chain rewrite; only the hub-held
    signatures can).  On an unmanned clinical box that means a tampered audit
    log is invisible forever.

    This puts the check on a daemon thread at a fraction of the scrape
    interval, so the failure shows up in the metrics feed an operator is
    already alerting on.  The work is one local file read plus one Ed25519
    verify per line — cheap, unlike ``AuditLog.verify()``, which the metrics
    route deliberately avoids for DoS reasons; verification belongs on a
    timer, never in the scrape path.
    """

    def __init__(
        self,
        anchor_path: Path,
        public_key: str | bytes | Ed25519PublicKey,
        *,
        interval_sec: float = _DEFAULT_VERIFY_INTERVAL_SEC,
        initial_delay_sec: float = _DEFAULT_VERIFY_INITIAL_DELAY_SEC,
        on_failure: Callable[[AnchorVerification], None] | None = None,
    ) -> None:
        self._path = Path(anchor_path)
        self._public_key = public_key
        self._interval = interval_sec
        self._initial_delay = initial_delay_sec
        self._on_failure = on_failure
        self._last: AnchorVerification | None = None
        self._failures_total = 0
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # ── results the metrics route reads (never blocks, never raises) ─────

    @property
    def last_result(self) -> AnchorVerification | None:
        """The most recent pass; None until the first one completes."""
        return self._last

    @property
    def failures_total(self) -> int:
        """Anchor lines that failed verification since process start."""
        return self._failures_total

    @property
    def interval_sec(self) -> float:
        """The cadence the timer runs at (logged once at boot)."""
        return self._interval

    # ── lifecycle ────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the background verification timer (idempotent)."""
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._work, name="audit-anchor-verifier", daemon=True
        )
        self._thread.start()

    def stop(self, *, join_timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=join_timeout)
            self._thread = None

    def verify_now(self) -> AnchorVerification:
        """Run one verification pass synchronously; records and reports it.

        Never raises: a verifier that takes down the caller would be worse
        than a silent anchor, and every failure mode here is a *finding*.
        """
        try:
            if not self._path.exists():
                result = AnchorVerification(ok=True, errors=(), file_absent=True)
            else:
                ok, errors = verify_anchor_signatures(self._path, self._public_key)
                result = AnchorVerification(ok=ok, errors=tuple(errors))
        except Exception:  # noqa: BLE001 — a crash here must not kill the timer
            logger.exception("audit anchor verification failed unexpectedly")
            result = AnchorVerification(ok=False, errors=())
        self._last = result
        if not result.ok:
            self._failures_total += len(result.errors) or 1
            if self._on_failure is not None:
                try:
                    self._on_failure(result)
                except Exception:  # noqa: BLE001 — reporting must not kill the timer
                    logger.exception("anchor failure callback raised")
        return result

    def _work(self) -> None:
        # Let the boot settle (and the first heads land) before the first read;
        # afterwards the interval is the cadence an operator alerts on.
        self._stop_event.wait(self._initial_delay)
        while not self._stop_event.is_set():
            self.verify_now()
            self._stop_event.wait(self._interval)
