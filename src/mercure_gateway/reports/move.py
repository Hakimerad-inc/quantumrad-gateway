"""Report retrieve transport — C-MOVE SCU (US-05, refinement §2.3, S05-T2).

Requests the PACS to send report instances back to the gateway's own C-STORE
SCP (run on an internal port, per the sprint note).  Received SR instances are
saved under ``reports/{study_uid}/sr/`` and PDF instances under
``reports/{study_uid}/pdf/``.

The retrieve flow:
1. Start the gateway's C-STORE SCP on ``store_scp_port``.
2. Issue one STUDY-level C-MOVE per distinct study in *matches*, naming the
   gateway as the move destination (the C-FIND answer is one row per report
   instance, but the query identifies a whole study).
3. The PACS pushes instances back via C-STORE; each one is saved to disk
   *inside* the C-STORE handler (store-before-ack, as the receiver does) so
   no decoded Dataset outlives the callback.
4. Return :class:`RetrievedReport` records with the saved file path.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydicom.dataset import FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian
from pynetdicom import AE, evt
from pynetdicom.association import Association

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

logger = logging.getLogger(__name__)

# pynetdicom status: Processing failure — the C-STORE sub-operation could not
# complete. Acking a failed store as success would leave the report row
# pointing at a file that does not exist, so this is what a persist failure
# returns to the PACS (same code the receiver uses).
_STORE_FAILURE_STATUS = 0xC120


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
        # Filled by the C-STORE handler on the association reactor thread and
        # read by retrieve() once the move is done; both are reset per
        # retrieve() so a reused instance starts clean.
        self._saved: list[RetrievedReport] = []
        self._failures: list[str] = []
        self._lock = threading.Lock()

    def retrieve(self, matches: list[ReportMatch]) -> list[RetrievedReport]:
        """C-MOVE each *match*'s study to the gateway store SCP and save it.

        Returns the saved :class:`RetrievedReport` records.  Raises
        :class:`ReportRetrieveError` when the PACS cannot be reached, rejects
        the association, or reports a C-MOVE failure — and when a received
        instance could not be persisted, so the caller never marks a report
        retrieved against a file that is not on disk.
        """
        if not matches:
            return []
        self.reports_dir.mkdir(parents=True, exist_ok=True)

        self._saved = []
        self._failures = []

        store_ae = AE(ae_title=self.store_scp_ae_title)
        store_ae.maximum_pdu_size = _MAX_PDU_SIZE
        store_ae.add_supported_context(SR_SOP_CLASS, ExplicitVRLittleEndian)
        store_ae.add_supported_context(PDF_SOP_CLASS, ExplicitVRLittleEndian)
        server = store_ae.start_server(
            ("", self.store_scp_port),
            evt_handlers=[(evt.EVT_C_STORE, self._on_c_store)],
            block=False,
        )
        if server is None:
            raise ReportRetrieveError("failed to start the gateway C-STORE SCP")

        ae = AE(ae_title=self.ae_title)
        ae.maximum_pdu_size = _MAX_PDU_SIZE
        # connection_timeout bounds the TCP connect phase (it defaults to None
        # — blocking — and is what AE.connect passes to settimeout before
        # socket.connect). A black-holed PACS host would otherwise pin this
        # thread until the OS stack gives up.
        ae.connection_timeout = self.timeout
        ae.acse_timeout = self.timeout
        ae.network_timeout = self.timeout
        ae.add_requested_context(_STUDY_ROOT_MOVE, ExplicitVRLittleEndian)

        # The store SCP is live from here on. Every exit must shut it down:
        # associate() can raise (bad address, TLS failure) and the release()
        # below used to never run, leaking the SCP thread and its port — and
        # with the port bound, the next retrieve could not start one at all.
        assoc: Association | None = None
        try:
            assoc = ae.associate(self.host, self.port, ae_title=self.aet)
            if not assoc.is_established:
                raise ReportRetrieveError(
                    f"C-MOVE association rejected by PACS at {self.host}:{self.port}"
                )

            # One STUDY-level C-MOVE per distinct study. The C-FIND answer is
            # one row per report instance, but _move_dataset carries only the
            # study UID — so the old per-match loop issued N byte-identical
            # full-study transfers for a study with N matched instances.
            moved_studies: set[str] = set()
            for match in matches:
                if match.study_uid in moved_studies:
                    continue
                moved_studies.add(match.study_uid)
                query = self._move_dataset(match)
                for status, _ in assoc.send_c_move(
                    query, self.store_scp_ae_title, _STUDY_ROOT_MOVE
                ):
                    if status and status.Status not in (0xFF00, 0x0000):
                        raise ReportRetrieveError(
                            f"C-MOVE failed for study {match.study_uid} "
                            f"(status {status.Status:04X})"
                        )
        finally:
            # release() is a no-op on a non-established association, so this is
            # safe for the rejected branch too; it is skipped entirely when
            # associate() itself raised and assoc was never bound.
            if assoc is not None:
                assoc.release()
            server.shutdown()

        # The instance did arrive and could not be persisted. The C-STORE was
        # already answered 0xC120 so the PACS knows; surfacing it here keeps
        # retrieve() from handing back a success built on a missing file.
        if self._failures:
            raise ReportRetrieveError(
                f"{len(self._failures)} received report instance(s) could not "
                f"be persisted under {self.reports_dir}: "
                f"{', '.join(self._failures[:5])}"
            )
        return list(self._saved)

    def _move_dataset(self, match: ReportMatch) -> Any:
        """Build a Study Root C-MOVE query dataset for *match*."""
        from pydicom.dataset import Dataset

        ds = Dataset()
        ds.QueryRetrieveLevel = "STUDY"
        ds.StudyInstanceUID = match.study_uid
        return ds

    def _on_c_store(self, event: evt.Event) -> int:
        """Handle an incoming C-STORE request; persist the instance before acking.

        Mirrors :meth:`mercure_gateway.receiver.Receiver._on_c_store`: the file
        is written inside the handler, so no decoded Dataset is retained after
        the callback returns (the old code buffered every instance in a dict
        and only flushed them once the association had been released).

        Returns 0x0000 on success and 0xC120 (Processing failure) when the
        instance could not be persisted — acking a failed store as success
        would leave the report row pointing at a file that does not exist.
        """
        ds = event.dataset
        # The UIDs come from the dataset itself, never from the C-FIND match:
        # a STUDY-level move lets the PACS push instances the match list never
        # mentioned, and keying the path off the match would save one instance
        # under another's name.
        sop_class = str(getattr(ds, "SOPClassUID", ""))
        if sop_class not in (SR_SOP_CLASS, PDF_SOP_CLASS):
            # Unreachable in practice — the store SCP advertises only the two
            # report contexts, so a non-report class cannot negotiate a
            # sub-association. Guard anyway: anything else would be filed
            # under ``pdf/`` by the branch below.
            logger.warning(
                "ignoring non-report instance (SOPClassUID=%s)", sop_class or "<absent>"
            )
            return 0x0000
        try:
            path = self._save(ds)
        except Exception:
            logger.exception(
                "failed to persist received report instance (SOPInstanceUID=%s) — "
                "returning processing failure",
                getattr(ds, "SOPInstanceUID", "<unknown>"),
            )
            with self._lock:
                self._failures.append(str(getattr(ds, "SOPInstanceUID", "<unknown>")))
            return _STORE_FAILURE_STATUS
        with self._lock:
            self._saved.append(
                RetrievedReport(
                    sop_class_uid=sop_class,
                    study_uid=str(getattr(ds, "StudyInstanceUID", "")),
                    sop_instance_uid=str(getattr(ds, "SOPInstanceUID", "")),
                    file_path=path,
                )
            )
        return 0x0000

    def _save(self, ds: Any) -> Path:
        """Persist *ds* under ``reports/{study_uid}/{sr|pdf}/{sop_uid}.dcm``.

        Every UID is read from *ds*: DIMSE carries them in the dataset (group
        0000), and the C-FIND match is neither necessary nor authoritative —
        a mismatched UID would have been written under the match's name with
        the match's ``file_meta``, and an instance absent from ``matches``
        was dropped outright.
        """
        # P0-1: the UIDs compose the output path, so a malicious or malformed
        # PACS puts its traversal payload in the dataset — this is where it is
        # rejected, before anything reaches the filesystem (same guard the
        # C-STORE receiver uses).
        study_uid = validate_uid(str(getattr(ds, "StudyInstanceUID", "")), what="StudyInstanceUID")
        sop_uid = validate_uid(str(getattr(ds, "SOPInstanceUID", "")), what="SOPInstanceUID")
        sop_class = str(getattr(ds, "SOPClassUID", ""))
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
