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

Durable delivery (TD-06): when a :class:`~mercure_gateway.spool.db.Database` is
injected, every ``feed()`` is first persisted to the ``hub_outbox`` table on the
same spool database.  Failed deliveries stay queued in memory *and* on disk;
``start()`` resumes the durable rows in insertion order, so audit events are
delivered at-least-once even across a crash/restart while the hub was down.
Durability never compromises the isolation invariant: a persistence failure
falls back to in-memory-only and ``feed()`` still returns immediately.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import requests

if TYPE_CHECKING:
    from mercure_gateway.spool.db import Database

__all__ = ["HubEventStreamer"]

logger = logging.getLogger(__name__)

_DEFAULT_MAX_QUEUE = 1000
_DEFAULT_MAX_BATCH = 100
_BASE_BACKOFF_SEC = 0.2
_MAX_BACKOFF_SEC = 30.0
_POLL_SEC = 0.05

# Deque entries are (outbox row id | None, payload).  A None id means the event
# is in-memory only (streamer created without a database, or a persistence
# failure at feed time) — such events cannot be resumed across a restart.
_Entry = tuple[int | None, dict[str, Any]]


class HubEventStreamer:
    """Non-blocking queue + background worker that POSTs audit events to the hub.

    ``feed()`` never blocks: events are appended to a bounded deque under a
    short lock.  A daemon worker drains the queue in batches and delivers them
    to ``{bookkeeper_url}/events`` with Token auth (HTTP Authorization scheme
    ``Token``, per TD-19).  Failed batches are
    re-queued at the head and retried with exponential backoff.

    When *database* is supplied (TD-06) each event is also written to the
    ``hub_outbox`` table before being queued; delivered rows are deleted,
    failed rows keep their attempt counter, and pending rows are resumed on
    ``start()``.
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
        database: Database | None = None,
    ) -> None:
        self._url = bookkeeper_url.rstrip("/")
        self._api_key = api_key
        self._gateway_name = gateway_name
        self._enabled = enabled
        self._max_queue = max_queue_size
        self._max_batch = max_batch_size
        # TD-06 follow-up: the durable outbox safety-net prune must never
        # delete a row that is still queued in memory — otherwise a crash
        # loses events the at-least-once guarantee promised to resume. The
        # most undelivered rows the streamer can legitimately hold is the
        # memory queue plus one in-flight batch, so the cap is derived from
        # those instead of a fixed constant below the queue bound.
        self._outbox_cap = max_queue_size + max_batch_size
        self._database = database
        self._deque: deque[_Entry] = deque()
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

    @property
    def is_running(self) -> bool:
        """True while the background delivery worker is active."""
        return self._running

    def start(self) -> None:
        """Start the background delivery worker (idempotent)."""
        if self._running:
            return
        self._resume_pending()
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._worker, daemon=True, name="hub-event-streamer"
        )
        self._thread.start()
        logger.info("hub event streamer started (bookkeeper %s)", self._url)

    def stop(self, *, join_timeout: float = 5.0) -> None:
        """Stop the worker; pending events stay queued and (durable) on disk."""
        self._running = False
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=join_timeout)
            self._thread = None

    def feed(self, event: str, detail: dict[str, Any] | None = None) -> None:
        """Queue one audit event for delivery.

        Never blocks the caller (US-10 isolation invariant).  When the queue is
        full the *oldest* undelivered event is dropped so memory stays bounded.
        In durable mode the event is committed to the outbox *before* it is
        queued; a persistence failure degrades gracefully to in-memory-only.

        The outbox INSERT (BEGIN IMMEDIATE + fsync) deliberately runs OUTSIDE
        ``self._lock``: the lock only guards the deque, and holding it across
        a spool-DB write couples C-STORE receive threads to DB contention
        (WAL checkpoints, forwarder bursts).  The evicted row's DELETE lands
        right after the queue op; if a crash lands between the two, the stale
        row is simply redelivered (at-least-once).
        """
        if not self._enabled or not self._running:
            return
        payload: dict[str, Any] = {
            "event": event,
            "detail": detail or {},
            "ts": datetime.now(UTC).isoformat(),
        }
        row_id: int | None = None
        if self._database is not None:
            try:
                row_id = self._database.enqueue_hub_event(
                    json.dumps(payload),
                    max_rows=self._outbox_cap,
                )
            except Exception:  # noqa: BLE001 — boundary: persistence
                logger.exception(
                    "hub event persistence failed; keeping event in memory only"
                )
                row_id = None
        with self._lock:
            evict_id: int | None = None
            if len(self._deque) >= self._max_queue:
                evict_id = self._deque.popleft()[0]
            self._deque.append((row_id, payload))
        if evict_id is not None and self._database is not None:
            try:
                self._database.delete_hub_events([evict_id])
            except Exception:  # noqa: BLE001
                logger.exception("failed to prune evicted hub outbox row")

    def flush(self, timeout: float = 5.0) -> None:
        """Wait until the queue drains (delivery succeeded) or ``timeout``."""
        deadline = time.monotonic() + timeout
        while self.queue_size > 0 and time.monotonic() < deadline:
            self._stop_event.wait(_POLL_SEC)

    # -- durable outbox (TD-06) ---------------------------------------------

    def _resume_pending(self) -> None:
        """Requeue durable outbox rows in insertion order (best-effort)."""
        if self._database is None:
            return
        resumed = 0
        try:
            # Load at most the memory bound: rows beyond it could never be
            # delivered this session, and loading them would trigger the
            # overflow prune below (deleting undelivered events — the exact
            # loss TD-06's at-least-once guarantee forbids). The remainder
            # stays on disk for the next resume.
            rows = self._database.load_pending_hub_events(
                limit=self._max_queue
            )
        except Exception:  # noqa: BLE001 — boundary: persistence
            logger.exception("cannot load pending hub events from outbox")
            return
        with self._lock:
            for row in rows:
                try:
                    payload = json.loads(row["payload"])
                except (TypeError, ValueError):
                    logger.warning("skipping corrupt hub outbox row %s", row["id"])
                    continue
                if not isinstance(payload, dict):
                    continue
                self._deque.append((row["id"], payload))
                resumed += 1
            overflow = len(self._deque) - self._max_queue
            if overflow > 0:
                dropped_ids: list[int | None] = [
                    self._deque.popleft()[0] for _ in range(overflow)
                ]
                logger.warning(
                    "outbox held %d pending events over the %d memory bound; "
                    "%d oldest dropped from the resume set",
                    len(rows), self._max_queue, overflow,
                )
                try:
                    self._database.delete_hub_events(
                        [i for i in dropped_ids if i is not None]
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("failed to prune overflowed hub outbox rows")
        if resumed:
            logger.info("resumed %d pending hub event(s) from durable outbox", resumed)

    def _on_delivered(self, batch: list[_Entry]) -> None:
        """Drop durable rows for a successfully delivered batch."""
        with self._lock:
            self._inflight -= len(batch)
        ids = [row_id for row_id, _ in batch if row_id is not None]
        if not self._database or not ids:
            return
        try:
            self._database.delete_hub_events(ids)
        except Exception:  # noqa: BLE001 — boundary: persistence
            # At-least-once: the row is redelivered on the next run instead of
            # being silently lost from the outbox.
            logger.exception(
                "failed to delete %d hub outbox row(s) after delivery", len(ids)
            )

    def _mark_failed(self, batch: list[_Entry], exc: Exception) -> None:
        """Record failed delivery attempts on the durable rows."""
        ids = [row_id for row_id, _ in batch if row_id is not None]
        if not self._database or not ids:
            return
        try:
            self._database.mark_hub_events_failed(ids, str(exc))
        except Exception:  # noqa: BLE001 — boundary: persistence
            logger.exception("failed to update hub outbox attempt counters")

    def _prune(self, entries: list[_Entry]) -> None:
        """Delete durable rows for entries evicted from the queue."""
        ids = [row_id for row_id, _ in entries if row_id is not None]
        if not self._database or not ids:
            return
        try:
            self._database.delete_hub_events(ids)
        except Exception:  # noqa: BLE001 — boundary: persistence
            logger.exception("failed to prune evicted hub outbox row(s)")

    # -- delivery worker ----------------------------------------------------

    def _drain(self, limit: int) -> list[_Entry]:
        """Pop up to ``limit`` events off the head of the queue."""
        with self._lock:
            batch = [self._deque.popleft() for _ in range(min(limit, len(self._deque)))]
            self._inflight += len(batch)
            return batch

    def _requeue_head(self, batch: list[_Entry]) -> list[_Entry]:
        """Put a failed batch back at the head, dropping newest if full.

        Returns the entries evicted from the queue (newest end) so the caller
        can prune their durable outbox rows.
        """
        dropped: list[_Entry] = []
        with self._lock:
            self._inflight -= len(batch)
            for row_id, payload in reversed(batch):
                if len(self._deque) >= self._max_queue:
                    dropped.append(self._deque.pop())
                self._deque.appendleft((row_id, payload))
        return dropped

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
                    json={
                        "gateway": self._gateway_name,
                        "events": [payload for _, payload in batch],
                    },
                    headers={"Authorization": f"Token {self._api_key}"},  # TD-19
                    timeout=30,
                )
                if not resp.ok:
                    raise requests.RequestException(
                        f"bookkeeper returned HTTP {resp.status_code}"
                    )
            except Exception as exc:  # noqa: BLE001 — boundary: bookkeeper
                logger.warning(
                    "hub event delivery failed (%s); %d event(s) requeued",
                    exc, len(batch),
                )
                dropped = self._requeue_head(batch)
                self._mark_failed(batch, exc)
                self._prune(dropped)
                self._stop_event.wait(backoff)
                backoff = min(backoff * 2.0, _MAX_BACKOFF_SEC)
            else:
                self._on_delivered(batch)
                backoff = _BASE_BACKOFF_SEC
