"""Operator console v0 service layer (refinement §7, Sprint 04 T6).

The console is a read-only dashboard (queue/status/logs/errors) served over
localhost:8080.  This module provides the **service layer** — a single
:class:`ConsoleService` that aggregates data from the spool, audit log and
rotating text log into one :class:`ConsoleDashboard` for the web endpoint.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mercure_gateway.audit import AuditLog

__all__ = ["ConsoleDashboard", "ConsoleService"]

_ERROR_EVENTS = frozenset({"FORWARD_ERROR", "STUDY_FAILED"})


@dataclass
class ConsoleDashboard:
    """Aggregated dashboard state for the operator console."""

    queue: dict[str, int] = field(default_factory=dict)
    recent_events: list[dict[str, Any]] = field(default_factory=list)
    recent_errors: list[dict[str, Any]] = field(default_factory=list)
    head_hash: str = ""
    text_log_tail: list[str] = field(default_factory=list)


class ConsoleService:
    """Read-only service that aggregates console dashboard data.

    Typical usage::

        service = ConsoleService(spool, AuditLog(spool.database),
                                 text_log_path=Path("/var/log/operations.log"))
        dash = service.dashboard()
    """

    def __init__(
        self,
        spool: Any,
        audit: AuditLog,
        *,
        text_log_path: str | Path | None = None,
        recent_events: int = 20,
        tail_lines: int = 20,
    ) -> None:
        self._spool = spool
        self._audit = audit
        self._text_log_path = Path(text_log_path) if text_log_path else None
        self._recent_events = recent_events
        self._tail_lines = tail_lines

    def dashboard(self) -> ConsoleDashboard:
        """Return a snapshot of the current gateway state."""
        counts = self._spool.count_states()
        queue = {
            "total": sum(counts.values()),
            "queued": counts.get("QUEUED", 0),
            "sending": counts.get("SENDING", 0),
            "sent": counts.get("SENT", 0),
            "error": counts.get("ERROR", 0),
            "failed": counts.get("FAILED", 0),
        }
        events = self._audit.list_events(limit=self._recent_events)
        recent_events = [
            {"id": e.id, "ts": e.ts, "event": e.event, "detail": e.detail, "user": e.user}
            for e in events
        ]
        recent_errors = [e for e in recent_events if e["event"] in _ERROR_EVENTS]
        text_log_tail = self._read_log_tail()
        return ConsoleDashboard(
            queue=queue,
            recent_events=recent_events,
            recent_errors=recent_errors,
            head_hash=self._audit.head_hash(),
            text_log_tail=text_log_tail,
        )

    def _read_log_tail(self) -> list[str]:
        """Return the last *tail_lines* lines of the rotating text log."""
        if self._text_log_path is None or not self._text_log_path.exists():
            return []
        try:
            with self._text_log_path.open("r", encoding="utf-8") as fh:
                lines = fh.readlines()
            return [line.rstrip("\n") for line in lines[-self._tail_lines :]]
        except OSError:
            return []
