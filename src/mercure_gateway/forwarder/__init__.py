"""Forwarding engine (PRD §5.2 step 3-4).

Reads tasks from the spool queue, dispatches to the configured destination
handlers, tracks per-destination status and applies retry with exponential
backoff up to ``retry_max`` attempts, after which the study is flagged
``FAILED`` and its local copy is retained (never auto-deleted; PRD §3.3/§3.4).

This is a skeleton: handlers per target type (dicom/sftp/rsync/...) are not
wired yet. The spool state machine transitions (claim → complete/fail) are
functional.
"""

from __future__ import annotations

import time

from mercure_gateway.config import GatewayConfig
from mercure_gateway.spool import ClaimedTask, Spool

__all__ = ["Forwarder", "RetryPolicy", "DeliveryResult"]


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

    def __init__(self, config: GatewayConfig, spool: Spool) -> None:
        self.config = config
        self.spool = spool
        self.retry = RetryPolicy()
        self._running = False

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
        """Claim up to ``limit`` tasks and dispatch each once.

        Returns the number of tasks processed. Transition bookkeeping (route
        complete/error, study SENT/ERROR/FAILED) is delegated to the spool.
        """
        raise NotImplementedError

    def _deliver(self, task: ClaimedTask) -> DeliveryResult:
        """Send one claimed task to its destination handler.

        The concrete transport is selected by ``task.target_type``; not
        implemented in the scaffold.
        """
        raise NotImplementedError

    def _await_retry(self, task: ClaimedTask) -> None:
        """Sleep according to the backoff policy before retrying ``task``."""
        attempts = self.spool.route_attempts(task.study_id, task.target_name)
        time.sleep(self.retry.delay(attempts))
