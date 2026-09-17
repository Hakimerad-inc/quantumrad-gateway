"""File-based rotating operations log alongside the SQLite audit chain (PRD §2.3).

The text log is distinct from the tamper-evident audit chain — it is a
conventional operations log for operators who want tail-able, human-readable
output.  It rotates at a configurable size threshold, keeps a fixed number of
backups and honours PHI scoping (§6.4): patient-identifying fields are stripped
from the ``phi`` dict when ``phi_scope`` is ``"minimal"`` (the default).
"""

from __future__ import annotations

import datetime
import json
import shutil
from pathlib import Path
from typing import Any

__all__ = ["TextLog"]

_PHI_FIELDS = frozenset({"patient_name", "mrn", "patient_id"})
_TS_FORMAT = "%Y-%m-%dT%H:%M:%S"


class TextLog:
    """Rotating text log for operators.

    Typical usage::

        log = TextLog(Path("/var/log/mercure-gateway/operations.log"))
        log.info("receiver started", phi={"ae_title": "GATEWAY", "port": 11112})
        log.error("forward failed: connection refused")
    """

    def __init__(
        self,
        path: Path,
        *,
        max_bytes: int = 10 * 1024 * 1024,
        max_backups: int = 5,
        phi_scope: str = "minimal",
    ) -> None:
        self._path = path
        self._max_bytes = max_bytes
        self._max_backups = max_backups
        self._phi_scope = phi_scope
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def info(self, message: str, phi: dict[str, Any] | None = None) -> None:
        """Write an INFO-level line."""
        self._write("INFO", message, phi)

    def error(self, message: str, phi: dict[str, Any] | None = None) -> None:
        """Write an ERROR-level line."""
        self._write("ERROR", message, phi)

    def _write(self, level: str, message: str, phi: dict[str, Any] | None = None) -> None:
        ts = datetime.datetime.now(datetime.UTC).strftime(_TS_FORMAT)
        phi_str = self._format_phi(phi) if phi else ""
        line = f"{ts} {level:<5} {message}{phi_str}\n"
        self._rotate_if_needed(len(line.encode("utf-8")))
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(line)

    def _format_phi(self, phi: dict[str, Any]) -> str:
        """Serialize *phi* as JSON, stripping PHI fields when scope is minimal."""
        if self._phi_scope == "minimal":
            phi = {k: v for k, v in phi.items() if k not in _PHI_FIELDS}
        if not phi:
            return ""
        return " " + json.dumps(phi, separators=(",", ":"), sort_keys=True)

    def _rotate_if_needed(self, line_bytes: int) -> None:
        """Rotate when the current file plus the new line exceeds the threshold."""
        if not self._path.exists():
            return
        current_size = self._path.stat().st_size
        if current_size + line_bytes <= self._max_bytes:
            return
        if self._max_backups <= 0:
            self._path.unlink(missing_ok=True)
            return
        # Remove the oldest backup if it exists.
        oldest = self._backup_path(self._max_backups)
        oldest.unlink(missing_ok=True)
        # Shift existing backups: .N → .N+1
        for i in range(self._max_backups - 1, 0, -1):
            src = self._backup_path(i)
            dst = self._backup_path(i + 1)
            if src.exists():
                shutil.move(str(src), str(dst))
        # Rename current → .1
        shutil.move(str(self._path), str(self._backup_path(1)))

    def _backup_path(self, n: int) -> Path:
        """Return the path of backup *n* (e.g. ``gateway.log.1``)."""
        return Path(f"{self._path}.{n}")
