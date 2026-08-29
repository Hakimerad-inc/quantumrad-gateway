"""Forwarding engine (PRD §5.2 step 3-4).

Reads tasks from the spool queue, dispatches to the registered destination
handlers, tracks per-destination status and applies retry with exponential
backoff up to ``retry_max`` attempts, after which the study is flagged
``FAILED`` and its local copy is retained (never auto-deleted; PRD §3.3/§3.4).

The spool state machine transitions (claim → complete/fail) and the
``process_once`` dispatch loop (with retry) are functional. Concrete
transports per target type (dicom/sftp/rsync/...) are implemented by separate
modules that satisfy the :class:`DestinationHandler` protocol and are
registered via :meth:`Forwarder.register_handler`.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Protocol

from mercure_gateway.config import GatewayConfig
from mercure_gateway.spool import ClaimedTask, Spool

__all__ = ["DestinationHandler", "Forwarder", "RetryPolicy", "DeliveryResult"]


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
    """Polls the spool, dispatches claimed tasks and applies retry/backoff."""

    def __init__(
        self,
        config: GatewayConfig,
        spool: Spool,
        *,
        retry: RetryPolicy | None = None,
    ) -> None:
        self.config = config
        self.spool = spool
        self.retry = retry or RetryPolicy()
        self._handlers: dict[str, DestinationHandler] = {}
        self._running = False

    def register_handler(self, target_type: str, handler: DestinationHandler) -> None:
        """Register the transport handler used for ``target_type``."""
        self._handlers[target_type] = handler

    def start(self) -> None:
        """Begin the polling loop that processes the queue continuously."""
        self._running = True

    def stop(self) -> None:
        """Stop the polling loop."""
        self._running = False

    @property
    def is_running(self) -> bool:
        """Whether the forwarding loop is active."""
        return self._running

    def process_once(self, limit: int = 1) -> int:
        """Claim up to ``limit`` tasks and dispatch each with retry.

        Returns the number of tasks processed. Transition bookkeeping (route
        complete/error, study SENT/ERROR/FAILED) is delegated to the spool.
        """
        tasks = self.spool.claim_next(limit)
        for task in tasks:
            self._dispatch_with_retry(task)
        return len(tasks)

    def _dispatch_with_retry(self, task: ClaimedTask) -> None:
        """Deliver ``task``, retrying with backoff until success or retry_max."""
        handler = self._handlers.get(task.target_type)
        if handler is None:
            self.spool.fail(
                task.study_id,
                task.target_name,
                f"no handler registered for target type {task.target_type!r}",
                max_attempts=self.retry.max_attempts,
            )
            return

        while True:
            result = handler.deliver(task, self.spool.spool_dir)
            if result.ok:
                self.spool.complete(task.study_id, task.target_name)
                return
            self.spool.fail(
                task.study_id,
                task.target_name,
                result.error or "delivery failed",
                max_attempts=self.retry.max_attempts,
            )
            if not self.retry.should_retry(
                self.spool.route_attempts(task.study_id, task.target_name)
            ):
                return
            self._await_retry(task)
            self.spool.reforward(task.study_id, task.target_name)
            reclaimed = self.spool.claim_next(1)
            if not reclaimed:
                return
            task = reclaimed[0]

    def _await_retry(self, task: ClaimedTask) -> None:
        """Sleep according to the backoff policy before retrying ``task``."""
        attempts = self.spool.route_attempts(task.study_id, task.target_name)
        time.sleep(self.retry.delay(attempts))
