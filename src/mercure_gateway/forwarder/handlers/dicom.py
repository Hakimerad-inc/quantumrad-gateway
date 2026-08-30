"""DICOM C-STORE SCU handler — ``dicom`` target type (PRD §5.2 step 3-4).

Satisfies the :class:`~mercure_gateway.forwarder.DestinationHandler` protocol.

Delivery reads files via :meth:`Spool.study_files` (the single source of
layout truth) and streams each instance's pre-encoded bytes without full
pydicom re-encoding. Any per-instance failure aborts the study-level task
with the offending file recorded in the error.

Transfer syntaxes: every standard syntax is *offered* on each negotiated
context (matching the receiver's accepted set) so studies stored with their
original compressed syntax (``receiver.decompress_common=false`` or a codec
fallback, refinement §2.1) can be forwarded as-is — review F7.
"""

from __future__ import annotations

from pathlib import Path

import pydicom
from pydicom.uid import (
    AllTransferSyntaxes,
    CTImageStorage,
    JPEGBaseline8Bit,
    JPEGLossless,
    MRImageStorage,
    RLELossless,
)
from pynetdicom import AE

from mercure_gateway.config import DICOMDestination
from mercure_gateway.forwarder import DeliveryResult
from mercure_gateway.spool import ClaimedTask, Spool

__all__ = ["DICOMHandler"]

# Default presentation contexts to negotiate with the SCP. All standard
# transfer syntaxes are offered per context (compressed + native).
_STORAGE_CONTEXTS = [
    CTImageStorage,
    MRImageStorage,
]

# Compressed syntaxes offered as dedicated per-syntax presentation contexts.
# DICOM allows the acceptor only ONE transfer syntax per accepted context and
# negotiation favors the first offered — so a multi-syntax request collapses
# to a single accepted syntax. A dedicated context per compressed syntax is
# the only way an as-received compressed study can be forwarded unchanged.
# (pydicom's ``AllTransferSyntaxes`` covers the rest, incl. JPEG 2000 /
# JPEG-LS / Deflated; these three are the ones it omits or that need a
# guaranteed dedicated slot.)
_COMPRESSED_SYNTAXES = [RLELossless, JPEGBaseline8Bit, JPEGLossless]

# Large PDUs are the single biggest throughput lever for C-STORE transfers.
_MAX_PDU_SIZE = 131072


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

        files = self.spool.study_files(study_uid)
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
        ae.maximum_pdu_size = _MAX_PDU_SIZE
        for ctx in _STORAGE_CONTEXTS:
            ae.add_requested_context(ctx, AllTransferSyntaxes)
        # Explicitly request dedicated contexts per compressed syntax so
        # studies stored as-received forward unchanged (F7).
        for syntax in _COMPRESSED_SYNTAXES:
            for ctx in _STORAGE_CONTEXTS:
                ae.add_requested_context(ctx, syntax)

        assoc = ae.associate(
            self.destination.host,
            self.destination.port,
            ae_title=self.destination.aet_target,
        )
        if not assoc.is_established:
            raise ConnectionError("association rejected by remote SCP")

        try:
            for path in files:
                ds = pydicom.dcmread(str(path), stop_before_pixels=False)
                status = assoc.send_c_store(ds)
                if status is None:
                    raise ConnectionError(f"C-STORE failed for {path.name}: no response")
                if status.Status not in (0x0000, 0xFF00):
                    raise ConnectionError(
                        f"C-STORE failed for {path.name}: status 0x{status.Status:04X}"
                    )
        finally:
            assoc.release()
