"""TDD (S03-T3, RED): main() runs one full receiver→spool→forwarder cycle.

Behaviors:
1. ``main()`` starts the receiver, binds the configured port, and accepts
   a C-STORE from a fake modality (receiver → spool).
2. The study is persisted in the spool DB (state RECEIVED).
3. ``main()`` exits 0 on SIGINT with graceful shutdown.

Coverage note (P1-6): these cases are the only ones that exercise main()'s
``__main__`` block and shutdown path, and they do it as real subprocesses —
which is why the composition root was invisible to the ≥80% gate for so long.
conftest.py:pytest_configure sets ``COVERAGE_PROCESS_START`` so the child's
lines are collected. A test added here that passes ``env=`` or ``cwd=`` to
Popen will silently break that collection: an inherited-cwd child finds the
config file, an overridden one does not.
"""

from __future__ import annotations

import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from mercure_gateway.config import (
    DICOMDestination,
    DICOMTLSDestination,
    DICOMwebDestination,
    FolderDestination,
    GatewayConfig,
    ReceiverConfig,
    RsyncDestination,
    S3Destination,
    SFTPDestination,
    XNATDestination,
    default_config,
    save_config,
)
from mercure_gateway.forwarder.handlers.dicom import DICOMHandler, DICOMTLSHandler
from mercure_gateway.forwarder.handlers.dicomweb import DICOMwebHandler
from mercure_gateway.forwarder.handlers.folder import FolderHandler
from mercure_gateway.forwarder.handlers.rsync import RsyncHandler
from mercure_gateway.forwarder.handlers.s3 import S3Handler
from mercure_gateway.forwarder.handlers.sftp import SFTPHandler
from mercure_gateway.forwarder.handlers.xnat import XNATHandler
from mercure_gateway.main import _build_forwarder
from mercure_gateway.spool.db import open_database


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wait_for_port(host: str, port: int, timeout: float | None = None) -> None:
    """Wait until a TCP port accepts connections.

    *timeout* defaults to 10 s, but when subprocess coverage is active
    (``COVERAGE_PROCESS_START`` set — see conftest.py) the child starts under
    ``sys.settrace``. pydicom's module-level import is pathologically
    trace-sensitive: 0.8 s with no tracer installed, ~18 s with *any* trace
    function (measured, not guessed — it is the first thing the child imports
    after stdlib). Without the extra budget these tests time out and the
    composition-root coverage P1-6 exists to collect never lands.
    """
    if timeout is None:
        timeout = 60.0 if os.environ.get("COVERAGE_PROCESS_START") else 10.0
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


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="send_signal(SIGINT) is POSIX; Windows shutdown covered by the clean-VM UAT",
)
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


# ── Integration: end-to-end boot with two destinations (review suggested work) ──


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="send_signal(SIGINT) is POSIX; Windows shutdown covered by the clean-VM UAT",
)
def test_main_end_to_end_two_destinations(tmp_path: Path) -> None:
    """main() boots with two destinations; a received study gets routes for both."""
    receiver_port = free_port()
    spool_dir = tmp_path / "spool"
    drop_dir = tmp_path / "drop"
    drop_dir.mkdir()

    cfg = default_config()
    cfg.receiver = ReceiverConfig(
        ae_title="GATEWAY", port=receiver_port, auto_enqueue_delay_sec=0.0
    )
    cfg.storage.spool_dir = str(spool_dir)
    cfg.destinations = [
        FolderDestination(name="drop", type="folder", path=str(drop_dir)),
        DICOMDestination(
            name="pacs", type="dicom", host="127.0.0.1", port=11112, aet_target="PACS"
        ),
    ]
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
        datasets = fake.create_synthetic_study("1.2.840.10008.99.2")
        result = fake.send_study(
            datasets,
            host="127.0.0.1",
            port=receiver_port,
            aet_target="GATEWAY",
        )
        assert result["success"] == 1
        assert result["failure"] == 0

        db_path = spool_dir / "mercure-gateway.db"
        db = open_database(db_path)
        studies = db.list_studies()
        assert len(studies) == 1
        assert studies[0]["study_uid"] == "1.2.840.10008.99.2"

        # With auto_enqueue_delay_sec=0, routes are created synchronously.
        routes = db.get_routes(studies[0]["id"])
        assert len(routes) == 2
        target_names = {r["target_name"] for r in routes}
        assert target_names == {"drop", "pacs"}
        db.close()
    finally:
        proc.send_signal(signal.SIGINT)
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate(timeout=5)

    assert proc.returncode == 0, f"main() exited {proc.returncode}: {stderr}"


# ── C3: every destination type is wired to a handler (review C3) ────────────
# Previously only ``dicom`` destinations registered a handler, so sftp/s3/…
# destinations were dead code that could never deliver a study. These tests
# pin the wiring end-to-end by inspecting the forwarder's handler registry.

_EXPECTED_HANDLERS = {
    "dicom": (DICOMDestination, DICOMHandler),
    "dicom_tls": (DICOMTLSDestination, DICOMTLSHandler),
    "dicomweb": (DICOMwebDestination, DICOMwebHandler),
    "sftp": (SFTPDestination, SFTPHandler),
    "rsync": (RsyncDestination, RsyncHandler),
    "s3": (S3Destination, S3Handler),
    "folder": (FolderDestination, FolderHandler),
    "xnat": (XNATDestination, XNATHandler),
}


def _one_destination_of_each_type() -> list:
    """Return one *enabled* destination of every supported type."""
    return [
        DICOMDestination(name="d1", host="127.0.0.1", port=11112, aet_target="PACS"),
        DICOMTLSDestination(name="d2", host="127.0.0.1", port=11112, aet_target="PACS"),
        DICOMwebDestination(name="d3", url="https://pacs.example/stow"),
        SFTPDestination(name="d4", host="127.0.0.1", username="u", remote_path="/r"),
        RsyncDestination(name="d5", host="127.0.0.1", username="u", remote_path="/r"),
        S3Destination(name="d6", bucket="b"),
        FolderDestination(name="d7", path="/tmp/out"),
        XNATDestination(name="d8", url="https://xnat", username="u", password="p", project="p"),
    ]


class _StubSpool:
    """Minimal spool stand-in: handlers only store it at construction time."""

    def __init__(self) -> None:
        self.spool_dir = "/tmp"


def _build(config: GatewayConfig):
    db = sqlite3.connect(":memory:")
    return _build_forwarder(config, _StubSpool(), db)


def test_build_forwarder_registers_every_destination_type() -> None:
    """Each enabled type resolves to exactly the right handler class."""
    cfg = default_config()
    cfg.destinations = _one_destination_of_each_type()
    forwarder = _build(cfg)

    for type_name, (_, handler_cls) in _EXPECTED_HANDLERS.items():
        matches = [k for k in forwarder._handlers if k.startswith(f"{type_name}:")]
        assert matches, f"no handler registered for type {type_name!r}"
        # The per-destination key must map to the correct handler class.
        dest = next(d for d in cfg.destinations if d.type == type_name)
        key = f"{type_name}:{dest.name}"
        assert key in forwarder._handlers, f"missing per-destination key {key!r}"
        assert isinstance(forwarder._handlers[key], handler_cls), (
            f"{key} mapped to {type(forwarder._handlers[key]).__name__}, "
            f"expected {handler_cls.__name__}"
        )


def test_build_forwarder_skips_disabled_destinations() -> None:
    """A disabled destination registers no handler (no silent dead routing)."""
    cfg = default_config()
    cfg.destinations = _one_destination_of_each_type()
    # Disable the sftp destination.
    for d in cfg.destinations:
        if d.type == "sftp":
            d.enabled = False
    forwarder = _build(cfg)

    assert all(
        not k.startswith("sftp:") for k in forwarder._handlers
    ), "disabled sftp destination should not register a handler"
    # The other seven are still wired.
    assert len(forwarder._handlers) == 7


def test_build_forwarder_registers_two_destinations_of_same_type() -> None:
    """Two enabled destinations of one type get distinct per-destination keys."""
    cfg = default_config()
    cfg.destinations = [
        DICOMDestination(name="a", host="127.0.0.1", port=11112, aet_target="PACS"),
        DICOMDestination(name="b", host="127.0.0.1", port=11113, aet_target="PACS2"),
    ]
    forwarder = _build(cfg)
    assert "dicom:a" in forwarder._handlers
    assert "dicom:b" in forwarder._handlers
    assert forwarder._handlers["dicom:a"].destination.port == 11112
    assert forwarder._handlers["dicom:b"].destination.port == 11113

# ── Update check wiring (ADR-0006 / review H2 followup) ──────────────────


def test_main_update_check_disabled_by_default(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    """No update config ⇒ the updater is never constructed (default off)."""
    import mercure_gateway.main as main_mod
    from mercure_gateway.config import UpdateConfig

    called = []
    monkeypatch.setattr(
        "mercure_gateway.update.Updater",
        lambda **kw: called.append(kw) or (_ for _ in ()).throw(AssertionError("constructed")),
    )
    cfg = default_config()
    assert cfg.update == UpdateConfig(), "default must be updates-disabled"
    main_mod._check_for_updates(cfg)
    assert called == []


def test_main_update_check_log_only_no_key(tmp_path, monkeypatch, caplog):  # type: ignore[no-untyped-def]
    """enabled but no public_key ⇒ fail-closed: updater never runs."""
    import logging

    import mercure_gateway.main as main_mod

    cfg = default_config()
    cfg.update.enabled = True
    cfg.update.update_url = "https://updates.example.com/latest.json"
    cfg.update.public_key = ""  # no trust anchor

    constructed = []
    monkeypatch.setattr(
        "mercure_gateway.update.Updater",
        lambda **kw: constructed.append(kw),
    )
    with caplog.at_level(logging.INFO):
        main_mod._check_for_updates(cfg)
    assert constructed == [], "must not construct an Updater without a trust anchor"


def test_main_update_check_runs_when_configured(tmp_path, monkeypatch, caplog):  # type: ignore[no-untyped-def]
    """enabled + url + key ⇒ check runs and logs (log-only, no auto-apply)."""
    import base64 as _b64
    import logging

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    import mercure_gateway.main as main_mod
    from mercure_gateway.update import UpdateResult

    key = Ed25519PrivateKey.generate()
    public_b64 = _b64.b64encode(
        key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ).decode("ascii")

    cfg = default_config()
    cfg.update.enabled = True
    cfg.update.update_url = "https://updates.example.com/latest.json"
    cfg.update.public_key = public_b64

    class FakeUpdater:
        def __init__(self, **kw):  # type: ignore[no-untyped-def]
            assert kw["public_key"] == public_b64
            assert kw["current_version"] == __import__(
                "mercure_gateway", fromlist=["__version__"]
            ).__version__

        def check_update(self):  # type: ignore[no-untyped-def]
            return UpdateResult(available=False)

    monkeypatch.setattr("mercure_gateway.update.Updater", lambda **kw: FakeUpdater(**kw))
    with caplog.at_level(logging.INFO):
        main_mod._check_for_updates(cfg)
    assert any("no update available" in r.message for r in caplog.records)


def test_main_update_check_never_raises(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    """A crashing updater must not break boot (boundary isolation)."""
    import mercure_gateway.main as main_mod

    cfg = default_config()
    cfg.update.enabled = True
    cfg.update.update_url = "https://updates.example.com/latest.json"
    cfg.update.public_key = "MCowBQYDK2VwAyEA"

    def boom(**kw):  # type: ignore[no-untyped-def]
        raise RuntimeError("network down")

    monkeypatch.setattr("mercure_gateway.update.Updater", lambda **kw: boom(**kw))
    assert main_mod._check_for_updates(cfg) is None  # no exception escaped


def _fake_getpass(monkeypatch: pytest.MonkeyPatch, answers: list[str]) -> None:
    """Answer getpass prompts in order; an extra prompt is a test bug."""
    import getpass

    it = iter(answers)

    def fake(prompt: str = "") -> str:
        try:
            return next(it)
        except StopIteration:
            raise AssertionError(f"unexpected getpass prompt: {prompt!r}") from None

    monkeypatch.setattr(getpass, "getpass", fake)


def test_set_web_password_stores_a_hash_and_enables_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The locked-out operator's only recovery — no API session required.

    The plaintext never touches disk: only the PBKDF2 hash is written, and the
    value is read from the tty (never the command line or a shell history).
    """
    import mercure_gateway.main as main_mod

    path = tmp_path / "gw.json"
    _fake_getpass(monkeypatch, ["s3cret-s3cret", "s3cret-s3cret"])

    rc = main_mod._set_web_password(path)

    assert rc == 0
    from mercure_gateway.config import load_config
    from mercure_gateway.web.auth import verify_password

    cfg = load_config(path)
    assert cfg.web_ui.auth_enabled is True
    assert verify_password("s3cret-s3cret", cfg.web_ui.auth_password_hash)
    # The plaintext is not on disk.
    assert "s3cret-s3cret" not in path.read_text()


def test_set_web_password_rejects_a_mismatched_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mercure_gateway.main as main_mod

    path = tmp_path / "gw.json"
    _fake_getpass(monkeypatch, ["s3cret-s3cret", "nope"])
    assert main_mod._set_web_password(path) == 1
    assert not path.exists()


def test_set_web_password_rejects_a_short_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mercure_gateway.main as main_mod

    path = tmp_path / "gw.json"
    # A too-short password is re-prompted, then cancelled by a mismatch.
    _fake_getpass(monkeypatch, ["short", "s3cret-s3cret", "nope"])
    assert main_mod._set_web_password(path) == 1
    assert not path.exists()


def test_set_web_password_cli_flag_reaches_the_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--set-web-password`` is wired into the entry point and exits 0."""
    import mercure_gateway.main as main_mod

    path = tmp_path / "gw.json"
    _fake_getpass(monkeypatch, ["s3cret-s3cret", "s3cret-s3cret"])
    rc = main_mod.main(["--config", str(path), "--set-web-password"])
    assert rc == 0
    assert path.exists()
