"""XNAT handler — REST upload to XNAT server (PRD §2.3 v1.1, S07-T4).

Satisfies the :class:`~mercure_gateway.forwarder.DestinationHandler` protocol.

Delivery uploads study files to an XNAT project via the XNAT REST API.
"""

from __future__ import annotations

from pathlib import Path

import requests

from mercure_gateway.config import XNATDestination
from mercure_gateway.forwarder import DeliveryResult
from mercure_gateway.spool import Spool

__all__ = ["XNATHandler"]


class XNATHandler:
    def __init__(self, destination: XNATDestination, spool: Spool) -> None:
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

        session = requests.Session()
        session.auth = (self.destination.username, self.destination.password)
        base = self.destination.url.rstrip("/")

        try:
            for f in files:
                resp = session.put(
                    f"{base}/REST/projects/{self.destination.project}/subjects/{study_uid}/experiments/{study_uid}/scans/{f.stem}/resources/{f.stem}/files/{f.name}",
                    data=f.read_bytes(),
                    headers={"Content-Type": "application/dicom"},
                )
                if not resp.ok:
                    return DeliveryResult(ok=False, error=f"XNAT upload failed: {resp.status_code}")
        except requests.RequestException as exc:
            return DeliveryResult(ok=False, error=str(exc))

        return DeliveryResult(ok=True)