"""Hub registration client (PRD §8.2, US-10, S08-T1).

On first run when configured, the gateway registers itself with the mercure
hub bookkeeper via ``POST /register-gateway``.  The registration is a one-time
handshake — once acknowledged, the gateway streams events via the event
streaming client (S08-T2).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import requests

__all__ = ["HubClient", "RegistrationResult"]

logger = logging.getLogger(__name__)

_DEFAULT_MAX_RETRIES = 3


@dataclass
class RegistrationResult:
    ok: bool = False
    skipped: bool = False


class HubClient:
    """Register this gateway with the mercure hub bookkeeper."""

    def __init__(
        self,
        bookkeeper_url: str,
        api_key: str,
        gateway_name: str,
        version: str,
        contact: str = "",
        *,
        enabled: bool = True,
        max_retries: int = _DEFAULT_MAX_RETRIES,
    ) -> None:
        self._url = bookkeeper_url.rstrip("/")
        self._api_key = api_key
        self._gateway_name = gateway_name
        self._version = version
        self._contact = contact
        self._enabled = enabled
        self.max_retries = max_retries

    def register(self) -> RegistrationResult:
        """POST the registration payload to the hub bookkeeper.

        Returns ``ok=True`` on a 2xx response, or after exhausting retries on
        5xx.  When ``enabled=False`` returns immediately with ``skipped=True``.
        """
        if not self._enabled:
            return RegistrationResult(ok=True, skipped=True)

        for _attempt in range(self.max_retries):
            try:
                resp = requests.post(
                    f"{self._url}/register-gateway",
                    json={
                        "name": self._gateway_name,
                        "version": self._version,
                        "contact": self._contact,
                    },
                    headers={"Authorization": f"Token {self._api_key}"},  # TD-19
                    timeout=30,
                )
                if resp.ok:
                    return RegistrationResult(ok=True)
                # Retry only on server errors (5xx)
                if 500 <= resp.status_code < 600:
                    continue
                # Non-5xx: fail immediately
                return RegistrationResult(ok=False)
            except Exception as exc:  # noqa: BLE001 — boundary: connection error
                logger.warning("hub registration attempt failed: %s", exc)
                continue

        return RegistrationResult(ok=False)
