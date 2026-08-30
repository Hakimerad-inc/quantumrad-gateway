"""Event streaming to hub bookkeeper (PRD §8.2, US-10, S08-T2).

The gateway streams lifecycle audit events to the mercure hub bookkeeper REST
endpoint (``POST /events``).  This module owns the **non-blocking isolation
invariant** (US-10): any reporting failure — bookkeeper down, timeout, or slow
response — must never block the receive/forward pipeline.

Design: :meth:`HubEventStreamer.feed` is a pure enqueue that holds a thread
lock for microseconds; a background worker drains the queue and POSTs batches
to the bookkeeper.  When a batch cannot be delivered the events are *re-queued
at the head* (never dropped) and the worker backs off exponentially, so a
downed hub slows only its own reporting — the forwarder's delivery timing and
count are untouched.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from datetime import UTC, datetime
from typing import Any

import requests

__all__ = ["HubEventStreamer"]

logger = logging.getLogger(__name__)

_DEFAULT_MAX_QUEUE = 1000
_DEFAULT_MAX_BATCH = 100
_BASE_BACKOFF_SEC = 0.2
_MAX_BACKOFF_SEC = 30.0
_POLL_SEC = 0.05


class HubEventStreamer:
    """Non-blocking queue + background worker that POSTs audit events to the hub.

    ``feed()`` never blocks: events are appended to an in-memory deque under a
    short lock.  A daemon worker drains the queue in batches and delivers them
    to ``{bookkeeper_url}/events`` with Bearer auth.  Failed batches are
    re-queued at the head and retried with exponential backoff.
    """

    def __init__(
        self,
        bookkeeper_url: str,
        api_key: str,
        gateway_name: str,
        *,
        enabled: bool = True,
        max_queue_size: int = _DEFAULT_MAX_QUEUE,
        max_batch_size: int = _DEFAULT_MAX_BATCH,
    ) -> None:
        self._url = bookkeeper_url.rstrip("/")
        self._api_key = api_key
        self._gateway_name = gateway_name
        self._enabled = enabled
        self._max_queue = max_queue_size
        self._max_batch = max_batch_size
        self._deque: deque[dict[str, Any]] = deque()
        self._inflight = 0
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._running = False

    @property
    def queue_size(self) -> int:
        """Total events pending delivery (queued + currently in flight)."""
        with self._lock:
            return len(self._deque) + self._inflight

    def start(self) -> None:
        """Start the background delivery worker (idempotent)."""
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._worker, daemon=True, name="hub-event-streamer"
        )
        self._thread.start()
        logger.info("hub event streamer started (bookkeeper %s)", self._url)

    def stop(self, *, join_timeout: float = 5.0) -> None:
        """Stop the worker; pending events are left in memory."""
        self._running = False
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=join_timeout)
            self._thread = None

    def feed(self, event: str, detail: dict[str, Any] | None = None) -> None:
        """Queue one audit event for delivery.

        Never blocks the caller (US-10 isolation invariant).  When the queue is
        full the *oldest* undelivered event is dropped so memory stays bounded.
        """
        if not self._enabled or not self._running:
            return
        payload: dict[str, Any] = {
            "event": event,
            "detail": detail or {},
            "ts": datetime.now(UTC).isoformat(),
        }
        with self._lock:
            if len(self._deque) >= self._max_queue:
                self._deque.popleft()
            self._deque.append(payload)

    def flush(self, timeout: float = 5.0) -> None:
        """Wait until the queue drains (delivery succeeded) or ``timeout``."""
        deadline = time.monotonic() + timeout
        while self.queue_size > 0 and time.monotonic() < deadline:
            self._stop_event.wait(_POLL_SEC)

    def _drain(self, limit: int) -> list[dict[str, Any]]:
        """Pop up to ``limit`` events off the head of the queue."""
        with self._lock:
            batch = [self._deque.popleft() for _ in range(min(limit, len(self._deque)))]
            self._inflight += len(batch)
            return batch

    def _requeue_head(self, batch: list[dict[str, Any]]) -> None:
        """Put a failed batch back at the head, dropping newest if full."""
        with self._lock:
            self._inflight -= len(batch)
            for payload in reversed(batch):
                if len(self._deque) >= self._max_queue:
                    self._deque.pop()
                self._deque.appendleft(payload)

    def _worker(self) -> None:
        backoff = _BASE_BACKOFF_SEC
        while self._running:
            batch = self._drain(self._max_batch)
            if not batch:
                self._stop_event.wait(_POLL_SEC)
                continue
            try:
                resp = requests.post(
                    f"{self._url}/events",
                    json={"gateway": self._gateway_name, "events": batch},
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    timeout=30,
                )
                if not resp.ok:
                    raise requests.RequestException(
                        f"bookkeeper returned HTTP {resp.status_code}"
                    )
            except Exception as exc:  # noqa: BLE001 — boundary: bookkeeper
                logger.warning(
                    "hub event delivery failed (%s); %d event(s) requeued", exc, len(batch)
                )
                self._requeue_head(batch)
                self._stop_event.wait(backoff)
                backoff = min(backoff * 2.0, _MAX_BACKOFF_SEC)
            else:
                with self._lock:
                    self._inflight -= len(batch)
                backoff = _BASE_BACKOFF_SEC
