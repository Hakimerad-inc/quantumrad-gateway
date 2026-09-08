"""rsync handler — rsync-over-SSH delivery (PRD §2.3 v1.1, S07-T4).

Satisfies the :class:`~mercure_gateway.forwarder.DestinationHandler` protocol.

Delivery copies study files to a remote path via the ``rsync`` CLI over SSH.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from mercure_gateway.config import RsyncDestination
from mercure_gateway.forwarder import DeliveryResult
from mercure_gateway.spool import Spool

__all__ = ["RsyncHandler"]


class RsyncHandler:
    def __init__(self, destination: RsyncDestination, spool: Spool) -> None:
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

        src_dir = spool_dir / study_uid
        remote_path = self.destination.remote_path.rstrip("/")
        remote = (
            f"{self.destination.username}@{self.destination.host}:"
            f"{remote_path}/{study_uid}/"
        )
        try:
            result = subprocess.run(
                ["rsync", "-az", "--no-o", "--no-g", str(src_dir) + "/", remote],
                capture_output=True,
                text=True,
                timeout=300,
            )
            if result.returncode != 0:
                return DeliveryResult(ok=False, error=result.stderr.strip())
        except subprocess.TimeoutExpired:
            return DeliveryResult(ok=False, error="rsync timed out")
        except OSError as exc:
            return DeliveryResult(ok=False, error=str(exc))

        return DeliveryResult(ok=True)