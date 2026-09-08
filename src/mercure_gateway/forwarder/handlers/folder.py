"""Folder handler — filesystem drop-folder delivery (PRD §2.3 v1.1, S07-T2).

Satisfies the :class:`~mercure_gateway.forwarder.DestinationHandler` protocol.

Delivery copies every DICOM file from the spooled study into the configured
target directory.  The target path may contain ``{study_uid}`` which is
expanded per study.  The operation is a **copy** — spool files remain intact
so the forwarder retry/recovery can work with the originals.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from mercure_gateway.config import FolderDestination
from mercure_gateway.forwarder import DeliveryResult
from mercure_gateway.spool import Spool

__all__ = ["FolderHandler"]


class FolderHandler:
    """Copy a study's DICOM files into a configured filesystem drop-folder.

    Usage::

        handler = FolderHandler(destination, spool)
        forwarder.register_handler("folder", handler)
    """

    def __init__(self, destination: FolderDestination, spool: Spool) -> None:
        self.destination = destination
        self.spool = spool

    def deliver(self, task: object, spool_dir: Path) -> DeliveryResult:
        """Copy the study's DICOM files into the target folder."""
        from mercure_gateway.spool import ClaimedTask

        assert isinstance(task, ClaimedTask)
        try:
            study_uid = self.spool.study_uid(task.study_id)
        except KeyError:
            return DeliveryResult(ok=False, error="study not found")

        files = self.spool.study_files(study_uid)
        if not files:
            return DeliveryResult(ok=False, error="no DICOM files found for study")

        # Always place the study in its own subdirectory so multiple studies
        # sharing a drop-folder never interleave.  An explicit ``{study_uid}``
        # template in the path is honored; otherwise we append it.
        base = Path(self.destination.path.replace("{study_uid}", study_uid))
        target = base / study_uid if "{study_uid}" not in self.destination.path else base
        try:
            target.mkdir(parents=True, exist_ok=True)
            for f in files:
                shutil.copy2(str(f), target / f.name)
        except OSError as exc:
            return DeliveryResult(ok=False, error=str(exc))

        return DeliveryResult(ok=True)