"""TDD (S01-T6, RED): Demo chain — fake modality → gateway → hub.

Behaviors:
1. ``DemoChain.run`` sends a synthetic study into a running gateway (receiver
   + forwarder wired to a hub SCP) and waits for it to be forwarded (SENT).
2. The hub (a second in-process Receiver) actually receives the study.
3. ``DemoChain.wait_for_forwarded`` returns before a timeout once SENT.
4. ``DemoChain.build_hub_config`` wires an Orthanc-as-hub DICOM destination
   from host/port/AET arguments.
"""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path
from typing import Any

from mercure_gateway.config import GatewayConfig, ReceiverConfig
from mercure_gateway.receiver import Receiver
from mercure_gateway.spool import Spool
from mercure_gateway.spool.db import mem_database


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _cfg(spool_dir: str, receiver_port: int) -> GatewayConfig:
    from mercure_gateway.config import default_config
    cfg = default_config()
    cfg.storage.spool_dir = spool_dir
    cfg.receiver = ReceiverConfig(ae_title="GATEWAY", port=receiver_port)
    return cfg


def make_spool(tmp_path: Path, port: int) -> Spool:
    return Spool(mem_database(), _cfg(str(tmp_path / "spool"), port))


# ── Test 1: build_hub_config wires an Orthanc destination ─────────────


def test_build_hub_config_wires_dicom_destination() -> None:
    from demo.demo_chain import DemoChain

    cfg = DemoChain.build_hub_config(
        host="127.0.0.1",
        port=4242,
        aet="ORTHANC",
    )

    assert len(cfg.destinations) == 1
    hub = cfg.destinations[0]
    assert hub.type == "dicom"
    assert hub.name == "hub"
    assert hub.host == "127.0.0.1"
    assert hub.port == 4242
    assert hub.aet_target == "ORTHANC"
    assert hub.enabled is True


# ── Test 2: Demo chain forwards a study to the hub ────────────────────


def test_demo_chain_forwards_study_to_hub(tmp_path: Path) -> None:
    from demo.demo_chain import DemoChain

    hub_port = free_port()
    gateway_port = free_port()

    # Hub SCP (stands in for Orthanc/mercure hub) — accepts C-STORE.
    hub_spool = make_spool(tmp_path / "hub", hub_port)
    hub = Receiver(ReceiverConfig(ae_title="ORTHANC", port=hub_port), hub_spool)
    hub.start()

    # Gateway: receiver + forwarder wired to the hub destination.
    gateway_spool = make_spool(tmp_path / "gateway", gateway_port)
    gateway = Receiver(ReceiverConfig(ae_title="GATEWAY", port=gateway_port), gateway_spool)
    gateway.start()

    from mercure_gateway.forwarder import Forwarder
    from mercure_gateway.forwarder.handlers.dicom import DICOMHandler

    cfg = _cfg(str(tmp_path / "gateway"), gateway_port)
    cfg = DemoChain.build_hub_config(
        host="127.0.0.1", port=hub_port, aet="ORTHANC", base=cfg
    )
    forwarder = Forwarder(cfg, gateway_spool)
    forwarder.register_handler("dicom", DICOMHandler(cfg.destinations[0], gateway_spool))
    forwarder.start()

    try:
        chain = DemoChain(
            gateway_config=cfg,
            receiver=gateway,
            forwarder=forwarder,
            study_uid="1.2.840.10008.99.1",
        )
        result = chain.run()

        assert result["sent"] == 1
        assert result["failure"] == 0

        # Gateway study reaches SENT (forwarded) state.
        state = gateway_spool.state(1)
        assert str(state) == "SENT"

        # Hub actually has the study.
        assert len(hub_spool._db.list_studies()) == 1
    finally:
        forwarder.stop()
        gateway.stop()
        hub.stop()


# ── Test 3: wait_for_forwarded returns when SENT, no spurious wait ────


def test_wait_for_forwarded_returns_on_sent(tmp_path: Path) -> None:
    from demo.demo_chain import DemoChain

    # No real network — a minimal spool whose state we control directly.
    spool = make_spool(tmp_path / "gw", free_port())
    from mercure_gateway.spool import StudyState
    study_id = spool.receive("1.2.840.10008.99.2")
    spool._db.set_study_state(study_id, StudyState.SENT.value)

    chain = DemoChain.__new__(DemoChain)
    chain.spool = spool
    chain.timeout_sec = 5.0

    start = time.monotonic()
    state = chain.wait_for_forwarded("1.2.840.10008.99.2")
    elapsed = time.monotonic() - start

    assert str(state) == "SENT"
    assert elapsed < 1.0  # no polling wait when already terminal


# ── Test 4: hub wait helper waits for the study to land ───────────────


def test_hub_wait_returns_when_study_arrives(tmp_path: Path) -> None:
    from demo.demo_chain import DemoChain

    spool = make_spool(tmp_path / "hub", free_port())
    chain = DemoChain.__new__(DemoChain)
    chain.hub_spool = spool
    chain.timeout_sec = 5.0

    def deliver() -> None:
        time.sleep(0.1)
        spool.receive("1.2.840.10008.99.3")

    threading.Thread(target=deliver, daemon=True).start()

    start = time.monotonic()
    row = chain.hub_received("1.2.840.10008.99.3")
    elapsed = time.monotonic() - start

    assert row is not None
    assert str(row["study_uid"]) == "1.2.840.10008.99.3"
    assert elapsed < 3.0


# ── Sanity: hub SCP accepts a C-STORE (guard for the demo) ────────────


def test_hub_accepts_cstore(tmp_path: Path) -> None:
    hub_port = free_port()
    hub_spool = make_spool(tmp_path / "hub", hub_port)
    hub = Receiver(ReceiverConfig(ae_title="ORTHANC", port=hub_port), hub_spool)
    hub.start()
    try:
        from demo.demo_chain import DemoChain
        from demo.fake_modality import FakeModality

        chain = DemoChain.__new__(DemoChain)
        chain.gateway_config = DemoChain.build_hub_config(
            host="127.0.0.1", port=hub_port, aet="ORTHANC"
        )
        chain._hub_forwarder = None  # type: ignore[attr-defined]
        result = FakeModality().send_study(
            FakeModality().create_synthetic_study("1.2.840.10008.99.4"),
            host="127.0.0.1",
            port=hub_port,
            aet_target="ORTHANC",
        )
        assert result["success"] == 1
        assert len(hub_spool._db.list_studies()) == 1
    finally:
        hub.stop()


# ── Test 5: demo chain CLI parses args and prints a summary ───────────


def test_demo_chain_cli_parses_arguments() -> None:
    from demo.demo_chain import _build_parser

    parser = _build_parser()
    args = parser.parse_args(
        [
            "--hub-host",
            "127.0.0.1",
            "--hub-port",
            "4242",
            "--hub-aet",
            "ORTHANC",
            "--study-uid",
            "1.2.840.10008.99.5",
            "--spool-dir",
            "/tmp/spool",
        ]
    )

    assert args.hub_host == "127.0.0.1"
    assert args.hub_port == 4242
    assert args.hub_aet == "ORTHANC"
    assert args.study_uid == "1.2.840.10008.99.5"
    assert args.spool_dir == "/tmp/spool"


def test_demo_chain_cli_main_returns_0(capsys, tmp_path: Path) -> None:
    """End-to-end CLI run: gateway → hub, then report pull over C-FIND."""
    from demo.demo_chain import main as demo_main

    hub_port = free_port()
    gateway_port = free_port()

    hub_spool = make_spool(tmp_path / "hub", hub_port)
    hub = Receiver(ReceiverConfig(ae_title="ORTHANC", port=hub_port), hub_spool)
    hub.start()

    # Report-pull SCP (stands in for Orthanc's C-FIND for the demo).
    from pydicom.dataset import Dataset
    from pydicom.uid import ExplicitVRLittleEndian
    from pynetdicom import AE, evt

    report_scp = AE(ae_title="ORTHANC")
    report_scp.add_supported_context("1.2.840.10008.5.1.4.1.2.2.1", ExplicitVRLittleEndian)

    def on_c_find(event: evt.Event) -> Any:
        ds = Dataset()
        ds.StudyInstanceUID = "1.2.840.10008.99.5"
        ds.AccessionNumber = "ACC-DEMO"
        ds.Modality = "CT"
        yield (0xFF00, ds)
        yield (0x0000, None)

    report_port = free_port()
    report_scp.start_server(
        ("", report_port), evt_handlers=[(evt.EVT_C_FIND, on_c_find)], block=False
    )

    try:
        rc = demo_main(
            [
                "--hub-host",
                "127.0.0.1",
                "--hub-port",
                str(hub_port),
                "--hub-aet",
                "ORTHANC",
                "--study-uid",
                "1.2.840.10008.99.5",
                "--spool-dir",
                str(tmp_path / "gateway"),
                "--receiver-port",
                str(gateway_port),
                "--report-host",
                "127.0.0.1",
                "--report-port",
                str(report_port),
                "--report-aet",
                "ORTHANC",
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "sent" in out
        assert "forwarded" in out
        assert "report" in out
        # Hub received the study.
        assert len(hub_spool._db.list_studies()) == 1
    finally:
        report_scp.shutdown()
        hub.stop()


# ── Test 6: demo chain retry mode ────────────────────────────────────


def test_demo_chain_retry_mode(capsys, tmp_path: Path) -> None:
    """--retry-mode simulates a delivery failure, re-forward, then success."""
    from demo.demo_chain import main as demo_main

    hub_port = free_port()
    gateway_port = free_port()

    hub_spool = make_spool(tmp_path / "hub", hub_port)
    hub = Receiver(ReceiverConfig(ae_title="ORTHANC", port=hub_port), hub_spool)
    hub.start()

    try:
        rc = demo_main(
            [
                "--hub-host",
                "127.0.0.1",
                "--hub-port",
                str(hub_port),
                "--hub-aet",
                "ORTHANC",
                "--study-uid",
                "1.2.840.10008.99.6",
                "--spool-dir",
                str(tmp_path / "gateway"),
                "--receiver-port",
                str(gateway_port),
                "--retry-mode",
                # The default 30s is tight when the whole suite runs concurrently
                # (send + 5s auto-enqueue debounce + retry dispatch under CPU
                # contention). Stay under the global pytest timeout (120s).
                "--timeout-sec",
                "90",
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "retried" in out
        assert "RETRY_MANUAL" in out
        assert "SENT" in out
        # Hub received the study after retry.
        assert len(hub_spool._db.list_studies()) == 1
    finally:
        hub.stop()