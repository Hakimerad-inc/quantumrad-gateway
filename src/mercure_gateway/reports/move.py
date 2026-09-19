"""Report retrieve transport — C-MOVE SCU (US-05, refinement §2.3, S05-T2).

Requests the PACS to send report instances back to the gateway's own C-STORE
SCP (run on an internal port, per the sprint note).  Received SR instances are
saved under ``reports/{study_uid}/sr/`` and PDF instances under
``reports/{study_uid}/pdf/``.

The retrieve flow:
1. Start the gateway's C-STORE SCP on ``store_scp_port``.
2. For each requested instance, issue a C-MOVE (Study Root) to the PACS,
   naming the gateway as the move destination.
3. The PACS pushes the instance back via C-STORE; it is saved to disk.
4. Return :class:`RetrievedReport` records with the saved file path.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydicom.dataset import FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian
from pynetdicom import AE, evt

from mercure_gateway.reports.find import PDF_SOP_CLASS, SR_SOP_CLASS, ReportMatch
from mercure_gateway.spool import validate_uid

__all__ = ["ReportRetrieve", "ReportRetrieveError", "RetrievedReport"]

# Study Root Query/Retrieve Information Model - MOVE (STUDY level).
_STUDY_ROOT_MOVE = "1.2.840.10008.5.1.4.1.2.2.2"
_MAX_PDU_SIZE = 131072

# Association budget — same correction as reports/find.py: pynetdicom 3.0.4's
# own defaults already bound a non-answering PACS; this makes the budget
# explicit and configurable (review P1-12).
_DEFAULT_ASSOCIATE_TIMEOUT_SEC = 30.0


class ReportRetrieveError(Exception):
    """Raised when the report C-MOVE cannot reach the PACS."""


@dataclass(frozen=True)
class RetrievedReport:
    """A report instance saved to disk after C-MOVE."""

    sop_class_uid: str
    study_uid: str
    sop_instance_uid: str
    file_path: Path

    @property
    def report_type(self) -> str:
        """Return ``"sr"`` or ``"pdf"`` for the saved report."""
        if self.sop_class_uid == SR_SOP_CLASS:
            return "sr"
        if self.sop_class_uid == PDF_SOP_CLASS:
            return "pdf"
        return "unknown"


class ReportRetrieve:
    """C-MOVE SCU that pulls report instances from the PACS into ``reports/``.

    Typical usage::

        retrieve = ReportRetrieve(host=..., port=104, aet="PACS",
                                  store_scp_port=5050, store_scp_ae_title="GATEWAY",
                                  reports_dir=Path("reports"))
        saved = retrieve.retrieve(matches)   # matches from ReportFinder.find()
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        aet: str,
        store_scp_port: int,
        store_scp_ae_title: str,
        reports_dir: Path,
        ae_title: str = "GATEWAY",
        timeout: float = _DEFAULT_ASSOCIATE_TIMEOUT_SEC,
    ) -> None:
        self.host = host
        self.port = port
        self.aet = aet
        self.store_scp_port = store_scp_port
        self.store_scp_ae_title = store_scp_ae_title
        self.reports_dir = Path(reports_dir)
        self.ae_title = ae_title
        self.timeout = timeout

    def retrieve(self, matches: list[ReportMatch]) -> list[RetrievedReport]:
        """C-MOVE each *match* to the gateway store SCP and save it to disk.

        Returns the saved :class:`RetrievedReport` records.  Raises
        :class:`ReportRetrieveError` when the PACS cannot be reached.
        """
        if not matches:
            return []
        self.reports_dir.mkdir(parents=True, exist_ok=True)

        saved: list[RetrievedReport] = []
        received_lock = threading.Lock()
        received: dict[str, Any] = {}

        def on_c_store(event: evt.Event) -> int:
            ds = event.dataset
            with received_lock:
                received[str(getattr(ds, "SOPInstanceUID", ""))] = ds
            return 0x0000

        store_ae = AE(ae_title=self.store_scp_ae_title)
        store_ae.maximum_pdu_size = _MAX_PDU_SIZE
        store_ae.add_supported_context(SR_SOP_CLASS, ExplicitVRLittleEndian)
        store_ae.add_supported_context(PDF_SOP_CLASS, ExplicitVRLittleEndian)
        server = store_ae.start_server(
            ("", self.store_scp_port),
            evt_handlers=[(evt.EVT_C_STORE, on_c_store)],
            block=False,
        )
        if server is None:
            raise ReportRetrieveError("failed to start the gateway C-STORE SCP")

        ae = AE(ae_title=self.ae_title)
        ae.maximum_pdu_size = _MAX_PDU_SIZE
        ae.acse_timeout = self.timeout
        ae.network_timeout = self.timeout
        ae.add_requested_context(_STUDY_ROOT_MOVE, ExplicitVRLittleEndian)
        assoc = ae.associate(self.host, self.port, ae_title=self.aet)
        if not assoc.is_established:
            server.shutdown()
            raise ReportRetrieveError(
                f"C-MOVE association rejected by PACS at {self.host}:{self.port}"
            )

        try:
            for match in matches:
                ds = self._move_dataset(match)
                for status, _ in assoc.send_c_move(ds, self.store_scp_ae_title, _STUDY_ROOT_MOVE):
                    if status and status.Status not in (0xFF00, 0x0000):
                        raise ReportRetrieveError(
                            f"C-MOVE failed for {match.sop_instance_uid} "
                            f"(status {status.Status:04X})"
                        )
            # Collect what arrived on the store SCP.
            with received_lock:
                stored = dict(received)
        finally:
            assoc.release()
            server.shutdown()

        for match in matches:
            ds = stored.get(match.sop_instance_uid)
            if ds is None:
                continue
            path = self._save(ds, match.study_uid, match.sop_class_uid, match.sop_instance_uid)
            saved.append(
                RetrievedReport(
                    sop_class_uid=match.sop_class_uid,
                    study_uid=match.study_uid,
                    sop_instance_uid=match.sop_instance_uid,
                    file_path=path,
                )
            )
        return saved

    def _move_dataset(self, match: ReportMatch) -> Any:
        """Build a Study Root C-MOVE query dataset for *match*."""
        from pydicom.dataset import Dataset

        ds = Dataset()
        ds.QueryRetrieveLevel = "STUDY"
        ds.StudyInstanceUID = match.study_uid
        return ds

    def _save(self, ds: Any, study_uid: str, sop_class: str, sop_uid: str) -> Path:
        """Persist *ds* under ``reports/{study_uid}/{sr|pdf}/{sop_uid}.dcm``."""
        # P0-1: the UIDs come from the PACS response and compose the output
        # path, so a malicious or malformed server can write outside
        # reports_dir. Reject anything that is not a bare DICOM UID before it
        # reaches the filesystem (same guard the C-STORE receiver uses).
        study_uid = validate_uid(study_uid, what="StudyInstanceUID")
        sop_uid = validate_uid(sop_uid, what="SOPInstanceUID")
        # A dataset arriving over the wire has NO file_meta — group 0002 is
        # file-format only and is never transmitted in DIMSE. The units tests
        # that covered this method handed it an in-memory dataset that had
        # one, which is why the gap was invisible: on a real retrieve
        # save_as(enforce_file_format=True) raised "Required File Meta
        # Information elements are either missing" before anything hit disk.
        # Rebuild it from the dataset itself, as the receiver does.
        if not getattr(ds, "file_meta", None):
            ds.file_meta = FileMetaDataset()
        ds.file_meta.MediaStorageSOPClassUID = sop_class
        ds.file_meta.MediaStorageSOPInstanceUID = sop_uid
        ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
        sub = "sr" if sop_class == SR_SOP_CLASS else "pdf"
        out_dir = self.reports_dir / study_uid / sub
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{sop_uid}.dcm"
        ds.save_as(str(path), enforce_file_format=True)
        return path
