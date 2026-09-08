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
    JPEGBaseline8Bit,
    JPEGLossless,
    RLELossless,
)
from pynetdicom import AE

from mercure_gateway.config import DICOMDestination, DICOMTLSDestination
from mercure_gateway.forwarder import DeliveryResult
from mercure_gateway.sop_classes import STORAGE_SOP_CLASSES, sop_classes_for_files
from mercure_gateway.spool import ClaimedTask, Spool

__all__ = ["DICOMHandler", "DICOMTLSHandler"]

# Fallback when a study's SOP classes cannot be determined (unreadable file,
# or a class outside :data:`STORAGE_SOP_CLASSES`). Offering all of them keeps
# delivery working rather than failing the task on a header we cannot parse.
_STORAGE_CONTEXTS = list(STORAGE_SOP_CLASSES)

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

    def _open_association(self, ae: AE) -> object:
        """Open the association to the destination. Overridden by the TLS variant."""
        return ae.associate(
            self.destination.host,
            self.destination.port,
            ae_title=self.destination.aet_target,
        )

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
        # Request only the SOP classes this study actually contains. Negotiating
        # all 111 classes × 4 syntaxes would blow the 128-context protocol
        # limit, and it used to request CT+MR only — which made every other
        # modality undeliverable (review C4).
        contexts = list(sop_classes_for_files(files)) or list(_STORAGE_CONTEXTS)
        for ctx in contexts:
            ae.add_requested_context(ctx, AllTransferSyntaxes)
        # Explicitly request dedicated contexts per compressed syntax so
        # studies stored as-received forward unchanged (F7).
        for syntax in _COMPRESSED_SYNTAXES:
            for ctx in contexts:
                ae.add_requested_context(ctx, syntax)

        assoc = self._open_association(ae)
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


class DICOMTLSHandler(DICOMHandler):
    """C-STORE SCU over DICOM-TLS (``dicom_tls`` target type, PRD §2.3 v1.1).

    Reuses the plain handler's file discovery and C-STORE loop; only the
    association is wrapped in a TLS context built from the destination's
    ``cacert`` / ``verify_peer`` settings. ``verify_peer`` enforces the server
    certificate (and its hostname); when it is off the channel is encrypted but
    the peer is not authenticated. A client certificate would be added by
    extending this class with ``ctx.load_cert_chain`` once the config carries
    one.
    """

    def __init__(self, destination: DICOMTLSDestination, spool: Spool) -> None:
        super().__init__(destination, spool)

    def _open_association(self, ae: AE) -> object:
        import ssl

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        if self.destination.cacert:
            ctx.load_verify_locations(self.destination.cacert)
        if self.destination.verify_peer:
            ctx.verify_mode = ssl.CERT_REQUIRED
            ctx.check_hostname = True
        else:
            ctx.verify_mode = ssl.CERT_NONE
            ctx.check_hostname = False

        return ae.associate(
            self.destination.host,
            self.destination.port,
            ae_title=self.destination.aet_target,
            tls_args=(ctx, self.destination.host),
        )
