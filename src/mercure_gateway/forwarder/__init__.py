"""Forwarding engine (PRD §5.2 step 3-4).

Reads tasks from the spool queue, dispatches to the registered destination
handlers, tracks per-destination status and applies retry with exponential
backoff until the retry budget is exhausted, after which the study is flagged
``FAILED`` and its local copy is retained (never auto-deleted; PRD §3.3/§3.4).

Retry budget: :class:`RetryPolicy` is built in code (``base_delay_sec=5.0``,
``max_attempts=5`` by default). There is no config knob for it — the
``retry`` argument to :meth:`Forwarder.__init__` is the only override, and
production runs the defaults.

Retry semantics: a failed task is failed through the state machine (route
``error``, study ``ERROR``) and *requeued*; the backoff delay is awaited on a
``threading.Event`` (interruptible by :meth:`Forwarder.stop`) and the route is
re-claimed by id in the *same* dispatch pass, not on a later poll. The
backoff is slept *before* the re-queue, so for the whole wait the route sits
in ``error`` and is invisible to ``claim_next`` — with more than one worker,
re-queueing first would let another worker claim it instantly and the
backoff would never apply. Re-claiming by route id can only return that same
route, so the loop can never dispatch a *different* task with the handler it
already resolved.

The spool state machine transitions (claim → complete/fail) and the
``process_once`` dispatch loop are functional. Concrete transports per target
type (dicom/sftp/rsync/...) are implemented by separate modules that satisfy
the :class:`DestinationHandler` protocol and are registered via
:meth:`Forwarder.register_handler`.

``start()`` owns a worker thread pool (``config.forwarding.concurrency``
workers polling every ``config.forwarding.queue_poll_interval_ms``);
``stop()`` interrupts sleeps and joins the workers.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Protocol

from mercure_gateway.audit import AuditLog
from mercure_gateway.audit.events import FORWARD_COMPLETE, FORWARD_ERROR, FORWARD_START
from mercure_gateway.config import GatewayConfig
from mercure_gateway.spool import ClaimedTask, Spool

__all__ = ["DestinationHandler", "Forwarder", "RetryPolicy", "DeliveryResult"]

logger = logging.getLogger(__name__)


class DestinationHandler(Protocol):
    """Protocol for a transport handler that delivers one study to a target.

    Implementations are registered per ``target_type`` (e.g. ``"dicom"``,
    ``"sftp"``).  Constructors receive the per-destination config so the
    public interface stays simple.
    """

    def deliver(self, task: ClaimedTask, spool_dir: Path) -> DeliveryResult:
        """Send the study referenced by ``task`` to the configured destination.

        Returns a :class:`DeliveryResult` indicating success or failure.
        """
        ...


class RetryPolicy:
    """Exponential backoff schedule: ``base_delay * 2 ** (attempt - 1)``."""

    def __init__(self, base_delay_sec: float = 5.0, max_attempts: int = 5) -> None:
        self.base_delay_sec = base_delay_sec
        self.max_attempts = max_attempts

    def delay(self, attempt: int) -> float:
        """Return the backoff delay (seconds) before the next retry."""
        exponent = max(0, attempt - 1)
        return self.base_delay_sec * float(2**exponent)

    def should_retry(self, attempt: int) -> bool:
        """Whether another retry is allowed after ``attempt`` failures."""
        return attempt < self.max_attempts


class DeliveryResult:
    """Outcome of attempting to deliver one task to one destination."""

    def __init__(self, ok: bool, error: str | None = None) -> None:
        self.ok = ok
        self.error = error


class Forwarder:
    """Polls the spool, dispatches claimed tasks and applies retry/backoff.

    In production call :meth:`start`, which runs ``process_once`` on a pool of
    worker threads at the configured poll cadence. ``process_once`` can also
    be driven manually (tests, one-shot retries).
    """

    def __init__(
        self,
        config: GatewayConfig,
        spool: Spool,
        *,
        retry: RetryPolicy | None = None,
        audit: AuditLog | None = None,
    ) -> None:
        self.config = config
        self.spool = spool
        self.retry = retry or RetryPolicy()
        self.audit = audit
        # Handler registry, keyed ``target_type`` (type-level fallback) and
        # ``"{target_type}:{target_name}"`` (per-destination override). With
        # two enabled ``dicom`` destinations a type-only key would collapse
        # every task onto the last-registered handler (misdelivery — review
        # F2), so dispatch prefers the destination key.
        self._handlers: dict[str, DestinationHandler] = {}
        self._running = False
        self._stop_event = threading.Event()
        self._workers: list[threading.Thread] = []

    def register_handler(
        self, target_type: str, handler: DestinationHandler, *, target_name: str | None = None
    ) -> None:
        """Register the transport handler for ``target_type``.

        Pass ``target_name`` to register a per-destination handler that takes
        precedence over the type-level fallback for that destination.
        """
        key = f"{target_type}:{target_name}" if target_name else target_type
        self._handlers[key] = handler

    def _resolve_handler(self, task: ClaimedTask) -> DestinationHandler | None:
        """Handler for *task*: destination-specific first, then type fallback."""
        specific = self._handlers.get(f"{task.target_type}:{task.target_name}")
        if specific is not None:
            return specific
        return self._handlers.get(task.target_type)

    def start(self) -> None:
        """Start the worker pool that processes the queue continuously."""
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        poll_sec = self.config.forwarding.queue_poll_interval_ms / 1000.0
        for i in range(self.config.forwarding.concurrency):
            t = threading.Thread(
                target=self._worker_loop, args=(poll_sec,), daemon=True, name=f"forwarder-{i}"
            )
            t.start()
            self._workers.append(t)
        logger.info(
            "Forwarder started (%d workers, poll %.1fs)",
            self.config.forwarding.concurrency,
            poll_sec,
        )

    def stop(self, *, join_timeout: float = 5.0) -> None:
        """Stop the worker pool; interrupts retry backoff waits.

        ``join_timeout`` bounds the join per worker: a worker inside a
        backoff wait wakes immediately (the stop event interrupts it), but a
        worker mid-``deliver`` blocks until the transport finishes — the join
        must not hang shutdown on a stuck socket. Workers are daemons, so a
        timed-out join abandons the in-flight route in ``error`` state where
        the operator can re-forward it from the console.
        """
        self._running = False
        self._stop_event.set()
        for t in self._workers:
            t.join(timeout=join_timeout)
        self._workers.clear()

    @property
    def is_running(self) -> bool:
        """Whether the forwarding loop is active."""
        return self._running

    def _worker_loop(self, poll_sec: float) -> None:
        """Poll-and-dispatch loop for one worker thread."""
        while self._running:
            try:
                self.process_once()
            except Exception:
                logger.exception("forwarder dispatch cycle failed")
            self._stop_event.wait(poll_sec)

    def process_once(self, limit: int = 1) -> int:
        """Claim up to ``limit`` tasks and dispatch each.

        Returns the number of tasks processed. Transition bookkeeping (route
        complete/error, study SENT/ERROR/FAILED) is delegated to the spool.
        """
        tasks = self.spool.claim_next(limit)
        for task in tasks:
            self._dispatch(task)
        return len(tasks)

    def _dispatch(self, task: ClaimedTask) -> None:
        """Deliver ``task``, retrying with backoff until success or budget end.

        Every exception from the handler is caught — one corrupt file or a
        misbehaving handler must never kill the dispatch loop (the route is
        failed through the state machine instead). Retries re-claim the route
        **by id**, so the loop can never drift onto a different task while
        keeping the resolved handler.
        """
        try:
            study_uid = self.spool.study_uid(task.study_id)
        except KeyError:
            study_uid = str(task.study_id)
        detail: dict[str, Any] = {
            "study_id": task.study_id,
            "study_uid": study_uid,
            "target_name": task.target_name,
            "target_type": task.target_type,
        }
        self._audit(FORWARD_START, detail)

        handler = self._resolve_handler(task)
        if handler is None:
            error = f"no handler registered for target type {task.target_type!r}"
            self._fail_task(task, error)
            self._audit(FORWARD_ERROR, {**detail, "error": error})
            return

        while True:
            try:
                result = handler.deliver(task, self.spool.spool_dir)
            except Exception as exc:  # noqa: BLE001 — handler boundary
                logger.exception(
                    "handler %r crashed delivering study %d", task.target_type, task.study_id
                )
                result = DeliveryResult(ok=False, error=f"{type(exc).__name__}: {exc}")

            if result.ok:
                self.spool.complete(task.study_id, task.target_name)
                self._audit(FORWARD_COMPLETE, detail)
                return

            self._fail_task(task, result.error or "delivery failed")
            attempts = self.spool.route_attempts(task.study_id, task.target_name)
            if not self.retry.should_retry(attempts):
                self._audit(FORWARD_ERROR, {**detail, "error": result.error or "delivery failed"})
                return

            # Sleep the backoff FIRST, then re-queue. Re-queuing before the
            # sleep would let another worker re-claim the route instantly,
            # so the exponential backoff never applied with >1 worker (F8):
            # while this worker sleeps, the route stays locked in 'sending'.
            if self._stop_event.wait(self.retry.delay(attempts)):
                # stop() requested during backoff: leave the route errored —
                # a manual re-forward (console/retry) can resume delivery.
                return
            self.spool.reforward(task.study_id, task.target_name)
            reclaimed = self.spool.claim_route(task.route_id)
            if reclaimed is None:
                return  # not claimable (failed elsewhere / route gone)
            task = reclaimed  # same route id — target_type unchanged

    def _audit(self, event: str, detail: dict[str, Any]) -> None:
        """Append an audit event when an AuditLog is wired; otherwise no-op."""
        if self.audit is not None:
            self.audit.append(event, detail)

    def _fail_task(self, task: ClaimedTask, error: str) -> None:
        """Record a delivery failure; final when the retry budget is spent."""
        self.spool.fail(
            task.study_id,
            task.target_name,
            error,
            max_attempts=self.retry.max_attempts,
        )
