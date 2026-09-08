"""DICOM C-STORE SCP receiver (PRD §5.2 step 1-2).

Accepts C-STORE associations from modalities, persists the received DICOM
files to the spool and extracts tags. The active transport backend is
``pynetdicom`` (ADR-0001 baseline; DCMTK ``storescp`` remains the documented
fallback — the transport contract is defined in
``tests/test_receiver_transport.py``).

Transfer syntaxes: **all** standard compressed syntaxes are accepted
(JPEG 2000 lossless/lossy, JPEG-LS, JPEG lossless SV1, RLE, Deflated —
refinement spec §2.1); selective decompression of common syntaxes is applied
at storage time when ``receiver.decompress_common`` is set.

Store-before-acknowledge (PRD §3.4): a C-STORE is acknowledged (0x0000) only
after the instance file and study row are durably written. Persist failures
return 0xC120 (processing failure) and are *logged* — a silent data-loss loop
is unacceptable for a clinical gateway.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any

from pydicom.uid import AllTransferSyntaxes
from pynetdicom import AE
from pynetdicom import evt as pynetdicom_evt
from pynetdicom.events import Event
from pynetdicom.sop_class import CTImageStorage, MRImageStorage  # type: ignore[attr-defined]

from mercure_gateway.config import ReceiverConfig
from mercure_gateway.spool import Spool

__all__ = ["Receiver"]

# SOP classes the SCP supports (CT/MR baseline — the PRD's modality-agnostic
# scope is tracked for expansion; see review note CR-037).
_STORAGE_CONTEXTS = [
    CTImageStorage,
    MRImageStorage,
]

# Large PDUs are the single biggest throughput lever for C-STORE transfers.
_MAX_PDU_SIZE = 131072

logger = logging.getLogger(__name__)


class Receiver:
    """C-STORE SCP that accepts associations and writes studies to the spool."""

    def __init__(self, config: ReceiverConfig, spool: Spool) -> None:
        self.config = config
        self.spool = spool
        self._running = False
        self._ae = AE(ae_title=self.config.ae_title)
        self._ae.maximum_pdu_size = _MAX_PDU_SIZE
        # US-01 AC: ≥25 concurrent modalities; pynetdicom default is 10.
        self._ae.maximum_associations = self.config.max_associations
        # Accept every standard transfer syntax (compressed + native) on each
        # supported SOP class — the refinement requires ALL compressed syntaxes
        # (JPEG 2000, JPEG-LS, RLE, Deflated, ...) end-to-end.
        for ctx in _STORAGE_CONTEXTS:
            self._ae.add_supported_context(ctx, AllTransferSyntaxes)
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
        else:
            logger.warning(
                "receiver.accepted_ae_titles is empty — accepting associations "
                "from ANY calling AE title. Configure an allow-list for "
                "production use."
            )
        # ``Sequence`` (covariant) rather than ``list`` for pynetdicom's
        # handler-tuple type; pynetdicom's stubs are stricter than the runtime.
        handlers: Sequence[tuple[Any, Callable[[Event], int]]] = [
            (pynetdicom_evt.EVT_C_STORE, self._on_c_store)
        ]
        self._server = self._ae.start_server(
            ("", self.config.port),
            evt_handlers=handlers,  # type: ignore[arg-type]
            block=False,
        )
        self._running = True
        logger.info("Receiver listening on port %d (AET %s)", self.port, self.config.ae_title)

    def stop(self) -> None:
        """Stop accepting associations and release the listening socket."""
        if self._server is not None:
            # pynetdicom leaks an association reactor thread stuck in
            # ``receive_pdu`` after every handled association. With
            # ``block_on_close=True`` (socketserver default) ``server_close``
            # joins it and hangs shutdown forever; the threads are daemonized
            # (they die at process exit), so skip the join.
            self._server.block_on_close = False
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
            logger.exception(
                "failed to persist received instance (SOPInstanceUID=%s) — "
                "returning processing failure",
                getattr(event.dataset, "SOPInstanceUID", "<unknown>"),
            )
            return 0xC120  # Processing failure
        return 0x0000
