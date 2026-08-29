"""DICOM C-STORE SCU handler — ``dicom`` target type (PRD §5.2 step 3-4).

Satisfies the :class:`~mercure_gateway.forwarder.DestinationHandler` protocol.
"""

from __future__ import annotations

from pathlib import Path

import pydicom
from pydicom.uid import CTImageStorage, MRImageStorage
from pynetdicom import AE

from mercure_gateway.config import DICOMDestination
from mercure_gateway.forwarder import DeliveryResult
from mercure_gateway.spool import ClaimedTask, Spool

__all__ = ["DICOMHandler"]

# Default presentation contexts to negotiate with the SCP
_STORAGE_CONTEXTS = [
    CTImageStorage,
    MRImageStorage,
]


class DICOMHandler:
    """C-STORE SCU that sends DICOM instances to a ``dicom`` destination.

    Usage::

        handler = DICOMHandler(destination, spool)
        forwarder.register_handler("dicom", handler)
    """

    def __init__(self, destination: DICOMDestination, spool: Spool) -> None:
        self.destination = destination
        self.spool = spool

    def deliver(self, task: ClaimedTask, spool_dir: Path) -> DeliveryResult:
        """Locate the study's DICOM files and C-STORE them to the destination."""
        try:
            study_uid = self.spool.study_uid(task.study_id)
        except KeyError:
            return DeliveryResult(ok=False, error="study not found")

        files = sorted((spool_dir / study_uid).rglob("*.dcm"))
        if not files:
            return DeliveryResult(ok=False, error="no DICOM files found for study")

        try:
            self._send_files(files)
        except ConnectionError as exc:
            return DeliveryResult(ok=False, error=str(exc))

        return DeliveryResult(ok=True)

    def _send_files(self, files: list[Path]) -> None:
        """Open a single association and send all files."""
        ae = AE(ae_title=self.destination.aet_source)
        for ctx in _STORAGE_CONTEXTS:
            ae.add_requested_context(ctx)

        assoc = ae.associate(
            self.destination.host,
            self.destination.port,
            ae_title=self.destination.aet_target,
        )
        if not assoc.is_established:
            raise ConnectionError("association rejected by remote SCP")

        try:
            for path in files:
                ds = pydicom.dcmread(str(path))
                status = assoc.send_c_store(ds)
                if status is None:
                    raise ConnectionError(f"C-STORE failed for {path.name}: no response")
                if status.Status not in (0x0000, 0xFF00):
                    raise ConnectionError(
                        f"C-STORE failed for {path.name}: status 0x{status.Status:04X}"
                    )
        finally:
            assoc.release()