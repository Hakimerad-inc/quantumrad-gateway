"""TDD (S02-T1, RED): receiver transport abstraction per ADR-0001.

The transport boundary separates *DICOM wire handling* (pynetdicom today,
DCMTK ``storescp`` per ADR-0001's fallback) from *ingest policy* (what the
gateway does with a received dataset). Behaviors:

1. A transport binds via ``start()`` and reports ``is_running``
2. ``on_study`` callback fires once per received dataset
3. ``stop()`` releases the port cleanly (idempotent)
4. The in-process fake transport drives the same contract without sockets
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Protocol

import pytest
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian

from mercure_gateway.config import ReceiverConfig
from mercure_gateway.spool import Spool
from mercure_gateway.spool.db import mem_database


class ReceiverTransport(Protocol):
    """Wire-protocol boundary for the C-STORE receiver (ADR-0001).

    Implementations bind a port (or run in-process) and invoke the ingest
    callback once per received DICOM dataset. The gateway core never touches
    pynetdicom directly — it programs this protocol.
    """

    @property
    def is_running(self) -> bool: ...

    def start(self) -> None: ...

    def stop(self) -> None: ...


class IngestPolicy(Protocol):
    """What the gateway does with each received dataset (persist-before-ack)."""

    def on_dataset(self, dataset: Any) -> int:
        """Persist one dataset; return the DICOM status code to acknowledge."""
        ...


# ---------------------------------------------------------------------------
# The contract under test (fake transport — same contract pynetdicom must meet)
# ---------------------------------------------------------------------------


class FakeTransport:
    """In-process transport for tests: no sockets, drives the same contract."""

    def __init__(self, policy: IngestPolicy) -> None:
        self.policy = policy
        self._running = False
        self.datasets: list[Any] = []

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self) -> None:
        if self._running:
            raise RuntimeError("transport already running")
        self._running = True

    def stop(self) -> None:
        self._running = False

    def receive(self, dataset: Any) -> int:
        """Test hook: simulate an inbound dataset through the policy."""
        self.datasets.append(dataset)
        return self.policy.on_dataset(dataset)


class SpoolIngest:
    """Real ingest policy: persist-before-acknowledge via the Spool (PRD §3.4)."""

    def __init__(self, spool: Spool) -> None:
        self.spool = spool
        self.persist_failures: list[Exception] = []

    def on_dataset(self, dataset: Any) -> int:
        try:
            self.spool.store_instance(dataset)
        except Exception as exc:  # persist failed → do NOT ack success
            self.persist_failures.append(exc)
            return 0xC120  # Processing failure
        return 0x0000  # Success


def make_dataset(study_uid: str) -> Any:
    ds = pytest.importorskip("pydicom.dataset").Dataset()
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = f"{study_uid}.1"
    ds.SOPInstanceUID = f"{study_uid}.1.1"
    ds.SOPClassUID = CTImageStorage
    ds.file_meta = pytest.importorskip("pydicom.dataset").FileMetaDataset()
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    return ds


class TestTransportLifecycle:
    def test_start_binds_and_reports_running(self) -> None:
        spool = Spool(mem_database())
        transport = FakeTransport(SpoolIngest(spool))

        assert transport.is_running is False
        transport.start()

        assert transport.is_running is True

    def test_double_start_raises(self) -> None:
        spool = Spool(mem_database())
        transport = FakeTransport(SpoolIngest(spool))
        transport.start()

        with pytest.raises(RuntimeError, match="already running"):
            transport.start()

    def test_stop_releases_cleanly_and_is_idempotent(self) -> None:
        spool = Spool(mem_database())
        transport = FakeTransport(SpoolIngest(spool))
        transport.start()

        transport.stop()
        transport.stop()  # idempotent

        assert transport.is_running is False

    def test_stop_without_start_is_safe(self) -> None:
        spool = Spool(mem_database())
        transport = FakeTransport(SpoolIngest(spool))

        transport.stop()  # must not raise

        assert transport.is_running is False


class TestIngestCallback:
    def test_callback_fires_per_dataset(self) -> None:
        spool = Spool(mem_database())
        transport = FakeTransport(SpoolIngest(spool))
        transport.start()

        status = transport.receive(make_dataset("1.2.3.4"))

        assert status == 0x0000
        assert len(transport.datasets) == 1
        assert len(spool._db.list_studies()) == 1

    def test_persist_failure_returns_processing_failure(self) -> None:
        spool = Spool(mem_database())
        policy = SpoolIngest(spool)

        broken = make_dataset("1.2.3.4")
        del broken.StudyInstanceUID  # store_instance will raise

        status = policy.on_dataset(broken)

        assert status == 0xC120
        assert len(policy.persist_failures) == 1
        assert len(spool._db.list_studies()) == 0  # nothing persisted


class TestPynetdicomTransportSatisfiesProtocol:
    def test_receiver_satisfies_transport_protocol(self, tmp_path: Path) -> None:
        """The pynetdicom Receiver must be substitutable for the protocol."""
        from mercure_gateway.receiver import Receiver

        cfg = ReceiverConfig(ae_title="GATEWAY", port=0)
        spool = Spool(mem_database())
        receiver: ReceiverTransport = Receiver(cfg, spool)

        assert receiver.is_running is False
        receiver.start()
        assert receiver.is_running is True
        receiver.stop()
        assert receiver.is_running is False


class TestThreadedFake:
    def test_fake_transport_thread_safe_for_load_tests(self) -> None:
        """S02-T2's ≥25-association test needs a transport whose receive()
        can be called from many threads — the fake must survive that."""
        spool = Spool(mem_database())
        transport = FakeTransport(SpoolIngest(spool))
        transport.start()

        results: list[int] = []
        lock = threading.Lock()

        def send(i: int) -> None:
            status = transport.receive(make_dataset(f"1.2.3.{i}"))
            with lock:
                results.append(status)

        threads = [threading.Thread(target=send, args=(i,)) for i in range(25)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert results == [0x0000] * 25
        assert len(spool._db.list_studies()) == 25
