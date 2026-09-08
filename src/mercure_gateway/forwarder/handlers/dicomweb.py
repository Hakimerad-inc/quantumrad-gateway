"""DICOMweb (STOW-RS) handler — HTTPS upload (PRD §2.3 v1.1, S07-T5).

Satisfies the :class:`~mercure_gateway.forwarder.DestinationHandler` protocol.

Delivery sends study DICOM instances to a STOW-RS endpoint via ``requests``.
The handler sends the raw DICOM file bytes as a multipart/related POST.
"""

from __future__ import annotations

from pathlib import Path

from mercure_gateway.config import DICOMwebDestination
from mercure_gateway.forwarder import DeliveryResult
from mercure_gateway.spool import Spool

__all__ = ["DICOMwebHandler"]


class DICOMwebHandler:
    def __init__(self, destination: DICOMwebDestination, spool: Spool) -> None:
        self.destination = destination
        self.spool = spool

    def deliver(self, task: object, spool_dir: Path) -> DeliveryResult:
        from mercure_gateway.spool import ClaimedTask

        assert isinstance(task, ClaimedTask)
        try:
            study_uid = self.spool.study_uid(task.study_id)
        except KeyError:
            return DeliveryResult(ok=False, error="study not found")

        files = self.spool.study_files(study_uid)
        if not files:
            return DeliveryResult(ok=False, error="no DICOM files found for study")

        import requests

        base_url = self.destination.url.rstrip("/")
        try:
            for f in files:
                raw = f.read_bytes()
                resp = requests.post(
                    url=f"{base_url}",
                    data=raw,
                    headers={
                        "Content-Type": "application/dicom",
                    },
                    timeout=60,
                )
                if not resp.ok:
                    return DeliveryResult(ok=False, error=f"STOW-RS failed: {resp.status_code}")
        except Exception as exc:  # noqa: BLE001 — boundary: map to DeliveryResult
            return DeliveryResult(ok=False, error=str(exc))

        return DeliveryResult(ok=True)