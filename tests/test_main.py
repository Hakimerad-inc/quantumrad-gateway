"""TDD (S03-T3, RED): main() runs one full receiver→spool→forwarder cycle.

Behaviors:
1. ``main()`` starts the receiver, binds the configured port, and accepts
   a C-STORE from a fake modality (receiver → spool).
2. The study is persisted in the spool DB (state RECEIVED).
3. ``main()`` exits 0 on SIGINT with graceful shutdown.
"""

from __future__ import annotations

import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from mercure_gateway.config import (
    GatewayConfig,
    ReceiverConfig,
    default_config,
    save_config,
)
from mercure_gateway.spool.db import open_database


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wait_for_port(host: str, port: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return
        except (OSError, ConnectionRefusedError):
            time.sleep(0.1)
    raise TimeoutError(f"port {port} not ready within {timeout}s")


def write_config(path: Path, cfg: GatewayConfig) -> None:
    save_config(cfg, path)


def test_main_receives_study_graceful_shutdown(tmp_path: Path) -> None:
    """main() starts the receiver, accepts a study, and exits 0 on SIGINT."""
    receiver_port = free_port()
    spool_dir = tmp_path / "spool"

    cfg = default_config()
    cfg.receiver = ReceiverConfig(ae_title="GATEWAY", port=receiver_port)
    cfg.storage.spool_dir = str(spool_dir)
    cfg.destinations = []  # no forwarding — just test the receive path
    config_path = tmp_path / "mercure-gateway.json"
    write_config(config_path, cfg)

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "mercure_gateway.main",
            "--config",
            str(config_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        wait_for_port("127.0.0.1", receiver_port)

        from demo.fake_modality import FakeModality

        fake = FakeModality(ae_title="TESTMODALITY")
        datasets = fake.create_synthetic_study("1.2.840.10008.99.1")
        result = fake.send_study(
            datasets,
            host="127.0.0.1",
            port=receiver_port,
            aet_target="GATEWAY",
        )
        assert result["success"] == 1
        assert result["failure"] == 0

        # Study should be persisted in the spool DB.
        db_path = spool_dir / "mercure-gateway.db"
        db = open_database(db_path)
        studies = db.list_studies()
        assert len(studies) == 1
        assert studies[0]["study_uid"] == "1.2.840.10008.99.1"
        db.close()
    finally:
        proc.send_signal(signal.SIGINT)
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate(timeout=5)

    assert proc.returncode == 0, f"main() exited {proc.returncode}: {stderr}"