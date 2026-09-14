"""Console entry point for mercure-gateway.

Loads configuration, opens the spool database, wires the store-and-forward
pipeline (receiver → spool → forwarder with DICOM handler per destination)
and optionally launches the web admin panel (FastAPI on localhost:8080).
The desktop shell is the Tauri wrapper per ADR-0002.

Composition root: this module owns construction and shutdown ordering of all
components — hub reporting, receiver, forwarder, hotplug detector, disk
monitor, web admin, database. Shutdown order is the reverse of construction:
disk monitor → web → hotplug → forwarder → receiver → report retriever → hub
streamer flush → database close.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import signal
import sys
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from mercure_gateway import __version__
from mercure_gateway.config import (
    GatewayConfig,
    apply_env_overrides,
    apply_usb_defaults,
    default_config,
    detect_usb_mode,
    load_config,
    save_config,
)
from mercure_gateway.disk import DiskMonitor
from mercure_gateway.forwarder import Forwarder
from mercure_gateway.hotplug import (
    HotplugDetector,
    run_shutdown_sequence,
    write_shutdown_marker,
)
from mercure_gateway.hub_client import HubClient
from mercure_gateway.hub_events import HubEventStreamer
from mercure_gateway.receiver import Receiver
from mercure_gateway.recovery import recover
from mercure_gateway.spool import Spool
from mercure_gateway.spool.db import Database, open_database
from mercure_gateway.textlog import TextLog

__all__ = ["main"]

logger = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quantumrad-gateway",
        description="Lightweight desktop DICOM gateway (store-and-forward).",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("mercure-gateway.json"),
        help="Path to mercure-gateway.json (default: ./mercure-gateway.json)",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"QuantumRAD Gateway {__version__}",
    )
    parser.add_argument(
        "--write-default-config",
        action="store_true",
        help="Write a default mercure-gateway.json and exit",
    )
    parser.add_argument(
        "--web",
        action="store_true",
        help="Launch the web admin panel (FastAPI on localhost:8080)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Web admin port (default: from config or 8080)",
    )
    return parser


# Bind hosts that are single-user by definition (web/auth.py + admin guide §Authentication
# treat loopback as the trusted default; everything else needs auth or the escape hatch).
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def _enforce_bind_security(config: GatewayConfig, environ: Mapping[str, str] | None = None) -> None:
    """Refuse to boot an unauthenticated admin panel on a non-loopback address.

    Security control (admin guide §Authentication, web/auth.py's no-op-when-auth-
    off argument rests on this): with ``web_ui.auth_enabled`` false the admin API
    — PHI, credentials, receiver/forwarder start/stop — is open to whoever can
    reach the bind address. The documented behavior is refusal; pre-D3b this only
    logged a warning and bound anyway. Loopback (127.0.0.1, localhost, ::1) is
    single-user by definition. Deliberate deployments (dev rigs, sidecar
    frontends) opt out explicitly via ``MERCURE_GATEWAY_ALLOW_INSECURE_BIND=1``,
    which downgrades the refusal to a loud startup warning.
    """
    ui = config.web_ui
    env = os.environ if environ is None else environ
    if ui.auth_enabled or ui.host in _LOOPBACK_HOSTS:
        return
    if env.get("MERCURE_GATEWAY_ALLOW_INSECURE_BIND", "") == "1":
        logger.warning(
            "web_ui.auth_enabled is false while binding to %s — the admin API "
            "(PHI, credentials, start/stop) is unauthenticated on the network. "
            "Proceeding because MERCURE_GATEWAY_ALLOW_INSECURE_BIND=1 is set; "
            "enable auth or bind to 127.0.0.1 for anything else.",
            ui.host,
        )
        return
    raise SystemExit(
        f"refusing to bind the web admin panel to {ui.host!r} while "
        "web_ui.auth_enabled is false: the API (PHI, credentials, start/stop) "
        "would be unauthenticated on the network. Enable web_ui auth (wizard → "
        "Setup) or set web_ui.host to 127.0.0.1. Escape hatch for deliberate "
        "deployments: MERCURE_GATEWAY_ALLOW_INSECURE_BIND=1."
    )


def _install_shutdown_signal_handlers(shutdown_done: threading.Event) -> None:
    """Force SIGINT/SIGTERM to trigger graceful headless shutdown.

    Python only installs its default ``KeyboardInterrupt`` handler at startup
    when SIGINT is not already ignored (POSIX background-job semantics). Under
    test harnesses and some service managers SIGINT can be inherited as
    ignored, which would silently defeat Ctrl+C shutdown. Explicitly
    installing a non-raising handler makes shutdown deterministic regardless
    of the inherited disposition.
    """

    def _request_shutdown(signum: int, _frame: Any) -> None:
        shutdown_done.set()

    signal.signal(signal.SIGINT, _request_shutdown)
    signal.signal(signal.SIGTERM, _request_shutdown)


def _build_forwarder(config: GatewayConfig, spool: Spool, database: Database) -> Forwarder:
    """Register a handler for every enabled destination.

    Every target type the config models (dicom, dicom_tls, dicomweb, sftp,
    rsync, s3, folder, xnat) is wired here — previously only ``dicom`` was, so
    a ``sftp``/``s3``/… destination could never be delivered (review C3).
    Handlers are registered *per destination* (``target_name``): a type-only
    registry would collapse two enabled destinations of the same type onto the
    last handler and misdeliver every study.
    """
    from mercure_gateway.audit import AuditLog
    from mercure_gateway.forwarder.handlers.dicom import DICOMHandler, DICOMTLSHandler
    from mercure_gateway.forwarder.handlers.dicomweb import DICOMwebHandler
    from mercure_gateway.forwarder.handlers.folder import FolderHandler
    from mercure_gateway.forwarder.handlers.rsync import RsyncHandler
    from mercure_gateway.forwarder.handlers.s3 import S3Handler
    from mercure_gateway.forwarder.handlers.sftp import SFTPHandler
    from mercure_gateway.forwarder.handlers.xnat import XNATHandler

    forwarder = Forwarder(config, spool, audit=AuditLog(database))
    for destination in config.destinations:
        if not destination.enabled:
            continue
        if destination.type == "dicom":
            forwarder.register_handler(
                "dicom", DICOMHandler(destination, spool), target_name=destination.name
            )
        elif destination.type == "dicom_tls":
            forwarder.register_handler(
                "dicom_tls", DICOMTLSHandler(destination, spool), target_name=destination.name
            )
        elif destination.type == "dicomweb":
            forwarder.register_handler(
                "dicomweb", DICOMwebHandler(destination, spool), target_name=destination.name
            )
        elif destination.type == "sftp":
            forwarder.register_handler(
                "sftp", SFTPHandler(destination, spool), target_name=destination.name
            )
        elif destination.type == "rsync":
            forwarder.register_handler(
                "rsync", RsyncHandler(destination, spool), target_name=destination.name
            )
        elif destination.type == "s3":
            forwarder.register_handler(
                "s3", S3Handler(destination, spool), target_name=destination.name
            )
        elif destination.type == "folder":
            forwarder.register_handler(
                "folder", FolderHandler(destination, spool), target_name=destination.name
            )
        elif destination.type == "xnat":
            forwarder.register_handler(
                "xnat", XNATHandler(destination, spool), target_name=destination.name
            )
        else:
            logger.warning(
                "no forwarding handler for destination type %r (destination %r)",
                destination.type,
                destination.name,
            )
    return forwarder


def _audit_anchor_path(config: GatewayConfig) -> Path:
    """Path of the append-only audit head-anchor file (review M4).

    Deliberately OUTSIDE the spool directory: the anchor's value is that an
    attacker who obtains/rewrites the spool (including the whole-disk case of
    a yanked USB dongle) does not also hold the anchored heads. Lives in the
    user's platform data dir next to nothing else the gateway writes, so a
    spool-level compromise does not reach it.
    """
    return Path.home() / ".local" / "share" / "mercure-gateway" / "audit-heads.txt"


def _signed_anchor_path(config: GatewayConfig) -> Path:
    """Path of the hub-signed anchor JSONL (review M4 signed-anchor followup).

    Sibling of :func:`_audit_anchor_path` — same outside-the-spool rationale.
    """
    return Path.home() / ".local" / "share" / "mercure-gateway" / "audit-heads-signed.jsonl"


def _wire_head_anchorer(config: GatewayConfig, audit: Any) -> Any:
    """Choose and attach the head anchorer for *audit* (review M4).

    Returns the anchorer object when it owns a lifecycle (SignedHeadAnchorer
    with a worker thread) so the caller can ``stop()`` it at shutdown, or
    ``None`` when a plain file anchorer (no lifecycle) was wired.
    """
    hub = config.audit.hub_reporting
    if hub.enabled and hub.bookkeeper_url and hub.anchor_public_key:
        # Hub-signed anchoring: file anchor + authenticated head via the
        # bookkeeper. The gateway holds no signing key (review M4).
        from mercure_gateway.audit.anchoring import SignedHeadAnchorer

        anchorer = SignedHeadAnchorer(
            hub.bookkeeper_url,
            hub.api_key,
            _audit_anchor_path(config),
            _signed_anchor_path(config),
            verify_key=hub.anchor_public_key,
            gateway_name=config.general.appliance_name,
        )
        anchorer.start()
        audit.set_head_anchorer(anchorer.anchor)
        return anchorer
    from mercure_gateway.audit import anchor_head_to_file

    audit.set_head_anchorer(anchor_head_to_file(_audit_anchor_path(config)))
    return None


def _check_for_updates(config: GatewayConfig) -> None:
    """Run the signed-update check at startup (ADR-0006, review H2 followup).

    Wires the previously-uninstantiated ``Updater`` into the composition root.
    Fail-closed: the check runs only when ``update.enabled`` is set AND both
    ``update_url`` and ``public_key`` are configured — without a trust anchor
    the updater rejects every archive, so the default is "updates off" rather
    than "updates unverified". The result is logged only: nothing is applied
    or downloaded without the operator explicitly triggering it.
    """
    upd = config.update
    if not upd.enabled or not upd.update_url or not upd.public_key:
        return
    try:
        from mercure_gateway.update import Updater

        updater = Updater(
            update_url=upd.update_url,
            current_version=__version__,
            public_key=upd.public_key,
        )
        result = updater.check_update()
    except Exception as exc:  # noqa: BLE001 — boundary: update check must not kill boot
        logger.warning("update check failed: %s", exc)
        return
    if result.error:
        logger.warning("update check error: %s", result.error)
    elif result.available and result.manifest is not None:
        logger.info(
            "update %s available (staging requires operator action via the admin API)",
            result.manifest.version,
        )
    else:
        logger.info("no update available (current %s)", __version__)


def _register_hub_in_background(hub_status: dict[str, Any], client: Any) -> None:
    """Register with the hub bookkeeper on a daemon thread (never blocks boot)."""

    def _run() -> None:
        try:
            result = client.register()
            hub_status["registered"] = bool(getattr(result, "ok", False))
        except Exception as exc:  # noqa: BLE001 — boundary: registration must not kill the gateway
            hub_status["registered"] = False
            hub_status["error"] = str(exc)
        finally:
            hub_status["registering"] = False

    threading.Thread(target=_run, name="hub-registration", daemon=True).start()


def _start_hub_reporting(
    config: GatewayConfig, audit: Any, *, database: Database | None = None
) -> tuple[dict[str, Any] | None, HubEventStreamer | None]:
    """Wire hub event streaming + registration when enabled; else ``(None, None)``.

    Returns a live hub-status dict (mutated by the background registration
    thread and read by the web admin panel) and the started streamer.  When hub
    reporting is disabled or under-configured, returns ``(None, None)``.

    ``database`` (TD-06) makes the event stream durable: each audit event is
    persisted to the ``hub_outbox`` table and resumed across restarts.
    """
    hub = config.audit.hub_reporting
    if not hub.enabled or not hub.bookkeeper_url:
        return None, None
    if not hub.api_key:
        logger.warning("audit.hub_reporting enabled but api_key is empty — hub reporting stays off")
        return None, None
    streamer = HubEventStreamer(
        hub.bookkeeper_url,
        hub.api_key,
        config.general.appliance_name,
        database=database,
    )
    streamer.start()
    audit.set_sink(lambda event, detail, _user: streamer.feed(event, detail))
    hub_status: dict[str, Any] = {
        "registered": False,
        "registering": True,
        "streaming": streamer.is_running,
        "bookkeeper_url": hub.bookkeeper_url,
    }
    client = HubClient(
        hub.bookkeeper_url,
        hub.api_key,
        config.general.appliance_name,
        __version__,
    )
    _register_hub_in_background(hub_status, client)
    logger.info("hub reporting enabled (bookkeeper %s)", hub.bookkeeper_url)
    return hub_status, streamer


def _run_web_admin(
    config: GatewayConfig,
    spool: Spool,
    receiver: Receiver,
    forwarder: Forwarder,
    report_retriever: Any,
    port: int,
    text_log: TextLog | None = None,
    config_path: Path | None = None,
    hub_status: dict[str, Any] | None = None,
) -> None:
    """Start the FastAPI web admin panel (blocking)."""
    import uvicorn

    from mercure_gateway.web import create_app

    app = create_app(config, spool, config_path=config_path)
    app.state.receiver = receiver
    app.state.forwarder = forwarder
    app.state.report_retriever = report_retriever
    app.state.hub_status = hub_status
    # Destination health monitor (pipeline view): daemon thread, stopped when
    # uvicorn exits the blocking call below.
    from mercure_gateway.web.pipeline import DestinationHealthMonitor

    health_monitor = DestinationHealthMonitor(config)
    health_monitor.start()
    app.state.health_monitor = health_monitor
    # Windows service management (S07-T9): None off Windows — the /api/service
    # endpoints degrade to available=false (GET) / 501 (POST).
    if sys.platform == "win32":
        from mercure_gateway.service_backend import WindowsServiceBackend
        from mercure_gateway.service_controller import ServiceController

        app.state.service_controller = ServiceController(WindowsServiceBackend())
    else:
        app.state.service_controller = None
    if text_log is not None:
        app.state.text_log_path = str(text_log._path)
    host = config.web_ui.host
    print(f"  web admin : http://{host}:{port}")
    try:
        uvicorn.run(app, host=host, port=port, log_level="info")
    finally:
        health_monitor.stop()


def main(argv: list[str] | None = None) -> int:
    """Run the gateway console entry point; returns a process exit code."""
    args = _build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.write_default_config:
        save_config(default_config(), args.config)
        print(f"Wrote default configuration to {args.config}")
        return 0

    config = default_config()
    if args.config.exists():
        config = load_config(args.config)
    else:
        save_config(config, args.config)
        print(f"No configuration found; wrote default to {args.config}")

    # 12-factor overrides: MERCURE_GATEWAY_* env vars win over the config file
    # (secrets such as the hub api_key can be injected without touching disk).
    config = apply_env_overrides(config)

    # USB variant auto-detection (S10-T5): when the spool directory lives on
    # removable media, adopt the USB profile — aggressive retention, disk-full
    # purge at 90 % capacity, storage budgeted to the data partition — before
    # any pipeline component is constructed from the config.
    if not config.usb_mode.enabled and detect_usb_mode(config.storage.spool_dir):
        config.usb_mode.enabled = True
        apply_usb_defaults(config)
        logger.info(
            "USB variant detected: spool on removable media — usb_mode.enabled; "
            "retention %dh, disk-full purge at 90%%",
            config.usb_mode.retention_delivered_hours,
        )

    _enforce_bind_security(config)
    # Signed-update check (ADR-0006): fail-closed, log-only, never blocks boot.
    _check_for_updates(config)

    spool_dir = Path(config.storage.spool_dir)
    spool_dir.mkdir(parents=True, exist_ok=True)
    # Activate the at-rest guard on the spool database (review H3): when a master
    # password is configured, open_database derives an HMAC verifier from it so a
    # database opened with a different/again key is refused (ADR-0004). True
    # SQLite *encryption* still requires SQLCipher — documented as a follow-up.
    from mercure_gateway.config.encryption import load_master_password

    database = open_database(spool_dir / "mercure-gateway.db", encrypt_key=load_master_password())
    from mercure_gateway.audit import AuditLog

    audit = AuditLog(database)
    # Local head anchor (review M4): every chain head is appended to an
    # external file so rewriting the DB alone cannot forge an intact chain.
    # The file lives OUTSIDE the spool directory (platform data dir) so a
    # spool-directory-level attacker (USB dongle yanked and inspected
    # elsewhere) does not get both the database and its anchor. When the hub
    # bookkeeper holds a signing key (hub_reporting.anchor_public_key set),
    # the anchorer additionally obtains an Ed25519 signature per head.
    head_anchorer = _wire_head_anchorer(config, audit)

    # Hub reporting (S08): streams every audit event to the bookkeeper and
    # registers the gateway in the background — boot never blocks on the hub.
    # TD-06: the event stream rides the spool database (durable outbox).
    hub_status, hub_streamer = _start_hub_reporting(config, audit, database=database)

    # Operations text log alongside the SQLite audit chain (S04-T3).
    text_log = TextLog(
        spool_dir / "operations.log",
        phi_scope=config.audit.phi_scope,
    )
    text_log.info("gateway starting")

    spool = Spool(database, config, audit=audit)

    # Retention: purge expired delivered studies on startup (US-04).
    purged = spool.purge_delivered()
    if purged:
        print(f"  retention : {purged} expired delivered study(ies) purged")

    # Recovery scan — reconcile spool files with DB only after an unclean
    # shutdown (the scan is skipped after a clean one so the DICOM port
    # binds immediately).
    recovery_result = recover(spool)
    if recovery_result.ran:
        print(
            f"  recovery  : {recovery_result.studies_recovered} recovered, "
            f"{recovery_result.files_without_db} orphaned files registered"
        )

    # Audit retention: prune old events on startup (S04-T7).
    pruned_audit = audit.prune(config.audit.retention_days)
    if pruned_audit:
        print(
            f"  audit     : {pruned_audit} event(s) pruned "
            f"(retention {config.audit.retention_days}d)"
        )

    receiver = Receiver(config.receiver, spool)
    receiver.start()

    forwarder = _build_forwarder(config, spool, database)
    forwarder.start()

    from mercure_gateway.reports import ReportRetriever

    report_retriever = ReportRetriever(config.reports, database, spool, audit=audit)
    report_retriever.start()

    shutdown_done = threading.Event()

    # Hot-unplug detection (USB mode only). The removal callback delegates to
    # the §7.2 sequence (hotplug.run_shutdown_sequence): receiver.stop →
    # bounded flush → fsynced shutdown marker, each step best-effort because
    # the device may vanish at any instant (review F10).
    def _on_usb_removal() -> None:
        run_shutdown_sequence(
            stop_receiver=receiver.stop,
            flush=lambda: hub_streamer.flush(timeout=5.0) if hub_streamer else None,
            spool_dir=spool.spool_dir,
            flush_timeout_sec=config.usb_mode.flush_timeout_sec,
        )
        print("\nUSB device removed — gateway shut down safely")
        shutdown_done.set()

    hotplug = HotplugDetector(
        spool.spool_dir,
        _on_usb_removal,
        enabled=config.usb_mode.enabled and config.usb_mode.hot_unplug_safe,
    )
    hotplug.start()

    disk_monitor = DiskMonitor(
        spool,
        warning_pct=config.storage.disk_full_warning_pct,
        purge_on_full=config.storage.purge_on_disk_full,
        max_spool_gb=config.storage.max_spool_gb,
    )
    disk_monitor.start()

    print(f"QuantumRAD Gateway {__version__}")
    print(f"  receiver  : AET={config.receiver.ae_title} port={config.receiver.port}")
    print(f"  forwarder : {forwarder.is_running and 'running' or 'stopped'}")
    print(f"  spool     : {spool.spool_dir}")
    print(f"  queued    : {spool.queued_count()} studies awaiting delivery")
    if hub_status is not None:
        print(f"  hub       : streaming to {config.audit.hub_reporting.bookkeeper_url}")

    exit_code = 0
    try:
        if args.web:
            web_port = args.port or config.web_ui.port
            try:
                _run_web_admin(
                    config,
                    spool,
                    receiver,
                    forwarder,
                    report_retriever,
                    web_port,
                    text_log,
                    config_path=args.config,
                    hub_status=hub_status,
                )
            except KeyboardInterrupt:
                print("\nShutting down web admin...")
        else:
            # Headless mode: run until interrupt or USB removal.
            _install_shutdown_signal_handlers(shutdown_done)
            with contextlib.suppress(KeyboardInterrupt):
                # Poll with a timeout so the handler (and any pending signal)
                # is processed on the next wake-up and graceful shutdown is
                # deterministic.
                while not shutdown_done.wait(timeout=1.0):
                    pass
    finally:
        disk_monitor.stop()
        hotplug.stop()
        forwarder.stop()
        receiver.stop()
        report_retriever.stop()
        if hub_streamer is not None:
            # Final flush so shutdown events reach the bookkeeper, then stop.
            hub_streamer.flush(timeout=5.0)
            hub_streamer.stop()
        if head_anchorer is not None:
            head_anchorer.flush(timeout=5.0)
            head_anchorer.stop()
        spool.stop()
        # Graceful shutdown marker: its presence lets the next boot skip the
        # recovery scan (a crash/power-loss leaves no marker → scan runs).
        with contextlib.suppress(OSError):
            write_shutdown_marker(spool.spool_dir)
        database.close()

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
