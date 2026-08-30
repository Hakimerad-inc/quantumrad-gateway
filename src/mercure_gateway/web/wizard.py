"""Web setup wizard state machine (PRD §2.2 Flow A, US-08, S06-T5).

Guides a non-technical user through initial gateway configuration in the SPA:

    receiver → destinations → reports → summary

Each step has a **validation gate**; a step cannot be completed until its data
passes.  The wizard can only be saved (``is_complete()``) once every step is
complete.  This module holds the pure state-machine logic; the SPA drives it
over ``GET/POST /api/wizard`` and the C-ECHO service (``web/echo.py``) powers
the per-step connectivity validation.
"""

from __future__ import annotations

from typing import Any

__all__ = ["SetupWizard"]

_STEPS = ["receiver", "destinations", "reports", "summary"]


class SetupWizard:
    """Guided setup state machine with per-step validation gates."""

    steps = list(_STEPS)

    def __init__(self) -> None:
        self._index = 0
        # step name → the data accepted when the step was completed
        self._completed: dict[str, dict[str, Any]] = {}

    # -- navigation -----------------------------------------------------

    def current_step(self) -> str:
        return self.steps[self._index]

    def next(self) -> None:
        if self._index < len(self.steps) - 1:
            self._index += 1

    def back(self) -> None:
        if self._index > 0:
            self._index -= 1

    # -- validation gates ----------------------------------------------

    def validate_step(self, step: str, data: dict[str, Any]) -> list[str]:
        """Return a list of human-readable validation errors for *step*.

        An empty list means the step's gate passes.
        """
        if step not in self.steps:
            raise KeyError(f"unknown wizard step {step!r}")
        if step == "receiver":
            return self._validate_receiver(data)
        if step == "destinations":
            return self._validate_destinations(data)
        if step == "reports":
            return self._validate_reports(data)
        if step == "summary":
            return self._validate_summary(data)
        return []

    def complete_step(self, step: str, data: dict[str, Any]) -> None:
        """Mark *step* as complete with its validated *data*.

        Raises ``ValueError`` when the step's gate fails, so callers cannot
        record an invalid step as done.
        """
        errors = self.validate_step(step, data)
        if errors:
            raise ValueError(f"step {step!r} failed validation: {'; '.join(errors)}")
        self._completed[step] = data

    def is_step_complete(self, step: str) -> bool:
        return step in self._completed

    def is_complete(self) -> bool:
        return all(step in self._completed for step in self.steps)

    # -- individual gates ----------------------------------------------

    @staticmethod
    def _validate_receiver(data: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        ae = str(data.get("ae_title", ""))
        if not ae or not ae.strip():
            errors.append("ae_title is required")
        port = data.get("port")
        if not isinstance(port, int) or not (1 <= port <= 65535):
            errors.append("port must be between 1 and 65535")
        return errors

    @staticmethod
    def _validate_destinations(data: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        destinations = data.get("destinations") or []
        if not destinations:
            return ["at least one destination is required"]
        for i, dest in enumerate(destinations):
            if not dest.get("name"):
                errors.append(f"destination {i + 1}: name is required")
            if not dest.get("host"):
                errors.append(f"destination {i + 1}: host is required")
            port = dest.get("port")
            if not isinstance(port, int) or not (1 <= port <= 65535):
                errors.append(f"destination {i + 1}: port must be between 1 and 65535")
        return errors

    @staticmethod
    def _validate_reports(data: dict[str, Any]) -> list[str]:
        # Optional step — enabled=False (or missing) always passes.
        if not data.get("enabled"):
            return []
        query_source = data.get("query_source")
        if not query_source:
            return ["query_source is required when reports are enabled"]
        return []

    def _validate_summary(self, data: dict[str, Any]) -> list[str]:
        required = [s for s in self.steps if s != "summary"]
        missing = [step for step in required if step not in self._completed]
        if missing:
            return [f"steps not completed: {', '.join(missing)}"]
        return []
