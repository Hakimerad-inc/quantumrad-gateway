"""DICOM C-STORE SCP receiver (PRD §5.2 step 1-2).

Accepts C-STORE associations from modalities, persists the received DICOM
files to the spool and extracts tags. Backends to evaluate in Phase 0 (PRD §9):
``pynetdicom`` (flexible SCP/SCU) or DCMTK ``storescp`` behind an abstraction
(PRD §11 risk mitigation). The active backend is ``pynetdicom``.
"""

from __future__ import annotations

from typing import Any

from pynetdicom import AE
from pynetdicom import evt as pynetdicom_evt
from pynetdicom.events import Event
from pynetdicom.sop_class import CTImageStorage, MRImageStorage

from mercure_gateway.config import ReceiverConfig
from mercure_gateway.spool import Spool

__all__ = ["Receiver"]

_STORAGE_CONTEXTS = [
    CTImageStorage,
    MRImageStorage,
]


class Receiver:
    """C-STORE SCP that accepts associations and writes studies to the spool."""

    def __init__(self, config: ReceiverConfig, spool: Spool) -> None:
        self.config = config
        self.spool = spool
        self._running = False
        self._ae = AE(ae_title=self.config.ae_title)
        for ctx in _STORAGE_CONTEXTS:
            self._ae.add_supported_context(ctx)
        self._server: Any | None = None

    @property
    def port(self) -> int:
        """The port the receiver is bound to (resolved after ``start``)."""
        if self._server is None:
            raise RuntimeError("receiver not started")
        return int(self._server.server_address[1])

    def start(self) -> None:
        """Bind the receiver to ``config.port`` / ``config.ae_title`` and begin
        accepting associations. Raises ``RuntimeError`` when already running."""
        if self._running:
            raise RuntimeError("receiver already running")
        if self.config.allowed_ae_titles:
            self._ae.require_calling_aet = self.config.allowed_ae_titles
        handlers = [(pynetdicom_evt.EVT_C_STORE, self._on_c_store)]
        self._server = self._ae.start_server(
            ("", self.config.port),
            evt_handlers=handlers,
            block=False,
        )
        self._running = True

    def stop(self) -> None:
        """Stop accepting associations and release the listening socket."""
        if self._server is not None:
            self._server.shutdown()
            self._server = None
        self._running = False

    @property
    def is_running(self) -> bool:
        """Whether the receiver is currently accepting associations."""
        return self._running

    def _on_c_store(self, event: Event) -> int:
        """Handle an incoming C-STORE request; persists instance before acking.

        Returns a pynetdicom status (0x0000 = Success). The store-before-ack
        guarantee (PRD §3.4) is satisfied because ``spool.store_instance``
        writes the file and study row before this handler returns.
        """
        try:
            self.spool.store_instance(event.dataset)
        except Exception:
            return 0xC120  # Processing failure
        return 0x0000
