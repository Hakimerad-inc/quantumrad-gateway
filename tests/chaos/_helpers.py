"""Shared doubles for the chaos suite (S09-T1).

:class:`FaultyStorageSCP` is an in-process DICOM C-STORE SCP whose behaviour can
be flipped mid-test to model a destination dying during a transfer: it accepts
a configured number of instances and then aborts the association — exactly the
failure a forwarder sees when a PACS goes down between instances (the SCU's
``send_c_store`` returns ``None``, which :class:`DICOMHandler` maps to a failed
delivery).

The rest are tiny factory helpers so the chaos scenarios read as scripts:
store a study, kill the destination, recover, re-forward.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import AllTransferSyntaxes, CTImageStorage, ExplicitVRLittleEndian, MRImageStorage
from pynetdicom import AE
from pynetdicom import evt as pynetdicom_evt
from pynetdicom.events import Event

from mercure_gateway.config import DICOMDestination, GatewayConfig, default_config
from mercure_gateway.forwarder import Forwarder, RetryPolicy
from mercure_gateway.forwarder.handlers.dicom import DICOMHandler
from mercure_gateway.spool import Spool
from mercure_gateway.spool.db import mem_database

__all__ = [
    "FaultyStorageSCP",
    "chaos_config",
    "chaos_spool",
    "make_dataset",
    "real_forwarder",
    "scp_destination",
]

_UID_BASE = "1.2.826.0.1.3680043.10"


def chaos_config(tmp_path: Path) -> GatewayConfig:
    """A real config: spool on *tmp_path*, auto-enqueue debounce parked far away
    so the chaos script drives the queue manually (no stray timers)."""
    cfg = default_config()
    cfg.storage.spool_dir = str(tmp_path / "spool")
    cfg.receiver.auto_enqueue_delay_sec = 3600
    return cfg


def chaos_spool(tmp_path: Path) -> tuple[Spool, GatewayConfig]:
    """Return ``(spool, config)`` wired to *tmp_path* (in-memory SQLite)."""
    cfg = chaos_config(tmp_path)
    return Spool(mem_database(), cfg), cfg


def make_dataset(study_uid: str, instance_idx: int = 1) -> Dataset:
    """Minimal valid Part-10 CT dataset (mirrors ``tests/test_storage.py``)."""
    ds = Dataset()
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = f"{study_uid}.1"
    ds.SOPInstanceUID = f"{study_uid}.1.{instance_idx}"
    ds.SOPClassUID = CTImageStorage
    ds.Modality = "CT"
    ds.PatientName = "CHAOS^TEST"
    ds.AccessionNumber = "A1"
    ds.file_meta = FileMetaDataset()
    ds.file_meta.MediaStorageSOPClassUID = CTImageStorage
    ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    return ds


class FaultyStorageSCP:
    """C-STORE SCP on localhost with a configurable mid-transfer crash."""

    def __init__(self, ae_title: str = "PACS") -> None:
        self.ae_title = ae_title
        self.received: list[str] = []
        self.crash_after: int | None = None
        self._ae = AE(ae_title=ae_title)
        self._ae.maximum_pdu_size = 131072
        for ctx in (CTImageStorage, MRImageStorage):
            self._ae.add_supported_context(ctx, AllTransferSyntaxes)
        self._server: Any | None = None

    @property
    def port(self) -> int:
        """The port the server is bound to (resolved after ``start``)."""
        if self._server is None:
            raise RuntimeError("SCP not started")
        return int(self._server.server_address[1])

    def start(self) -> None:
        """Bind an ephemeral localhost port and start accepting (no blocking)."""
        handlers: Sequence[tuple[Any, Any]] = [(pynetdicom_evt.EVT_C_STORE, self._on_store)]
        self._server = self._ae.start_server(
            ("127.0.0.1", 0),
            evt_handlers=handlers,  # type: ignore[arg-type]
            block=False,
        )

    def stop(self) -> None:
        """Stop accepting associations (mirrors Receiver.stop: skip the join —
        pynetdicom leaks a daemonized reactor thread)."""
        if self._server is not None:
            self._server.block_on_close = False
            self._server.shutdown()
            self._server = None

    def _on_store(self, event: Event) -> int:
        """Accept the requested instance, or kill the association to model a
        destination dying between instances of one study transfer."""
        if self.crash_after is not None and len(self.received) >= self.crash_after:
            event.assoc.abort()
            # Unreachable on the association we just aborted.
            return 0xB000
        self.received.append(str(event.dataset.SOPInstanceUID))
        return 0x0000


def scp_destination(scp: FaultyStorageSCP, *, name: str = "pacs") -> DICOMDestination:
    """A ``dicom`` destination pointing at the in-process SCP."""
    return DICOMDestination(
        name=name,
        type="dicom",
        host="127.0.0.1",
        port=scp.port,
        aet_target=scp.ae_title,
        aet_source="GATEWAY",
    )


def real_forwarder(
    spool: Spool,
    cfg: GatewayConfig,
    dest: DICOMDestination,
    *,
    max_attempts: int = 2,
) -> Forwarder:
    """A Forwarder with the real DICOM transport wired to ``dest``."""
    fwd = Forwarder(cfg, spool, retry=RetryPolicy(base_delay_sec=0, max_attempts=max_attempts))
    fwd.register_handler("dicom", DICOMHandler(dest, spool), target_name=dest.name)
    return fwd
