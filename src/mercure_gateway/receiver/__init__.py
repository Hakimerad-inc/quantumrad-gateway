"""DICOM C-STORE SCP receiver (PRD §5.2 step 1-2).

Accepts C-STORE associations from modalities, persists the received DICOM
files to the spool and extracts tags. This is a skeleton: the transport is not
wired yet. Backends to evaluate in Phase 0 (PRD §9): ``pynetdicom`` (flexible
SCP/SCU) or DCMTK ``storescp`` behind an abstraction (PRD §11 risk mitigation).
"""

from __future__ import annotations

from mercure_gateway.config import ReceiverConfig
from mercure_gateway.spool import Spool

__all__ = ["Receiver", "ReceivedStudy"]


class ReceivedStudy:
    """Metadata for a study accepted by the receiver and persisted to spool."""

    def __init__(self, study_uid: str, series: list[str], instances: int) -> None:
        self.study_uid = study_uid
        self.series = series
        self.instances = instances


class Receiver:
    """C-STORE SCP that accepts associations and writes studies to the spool.

    Not yet implemented — only the interface contract is defined.
    """

    def __init__(self, config: ReceiverConfig, spool: Spool) -> None:
        self.config = config
        self.spool = spool
        self._running = False

    def start(self) -> None:
        """Bind the receiver to ``config.port`` / ``config.ae_title`` and begin
        accepting associations. Raises ``RuntimeError`` when already running."""
        if self._running:
            raise RuntimeError("receiver already running")
        self._running = True

    def stop(self) -> None:
        """Stop accepting associations and release the listening socket."""
        self._running = False

    @property
    def is_running(self) -> bool:
        """Whether the receiver is currently accepting associations."""
        return self._running

    def _handle_c_store(self, study: ReceivedStudy) -> int:
        """Persist one received study to the spool and return its study id.

        The ``spool.receive(...)`` transition (RECEIVING → RECEIVED) is the
        store-before-acknowledge guarantee from PRD §3.4.
        """
        raise NotImplementedError
