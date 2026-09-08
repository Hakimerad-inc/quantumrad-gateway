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
import signal
import sys
import threading
from pathlib import Path
from typing import Any

from mercure_gateway import __version__
from mercure_gateway.config import (
    GatewayConfig,
    apply_env_overrides,
    default_config,
    load_config,
    save_config,
)
from mercure_gateway.disk import DiskMonitor
from mercure_gateway.forwarder import Forwarder
from mercure_gateway.forwarder.handlers.dicom import DICOMHandler
from mercure_gateway.hotplug import HotplugDetector, write_shutdown_marker
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
        prog="mercure-gateway",
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
        version=f"mercure-gateway {__version__}",
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


def _warn_insecure(config: GatewayConfig) -> None:
    """Warn about insecure web-panel settings at startup."""
    ui = config.web_ui
    if not ui.auth_enabled and ui.host not in ("127.0.0.1", "localhost"):
        logger.warning(
            "web_ui.auth_enabled is false while binding to %s — the admin API "
            "(PHI, credentials, start/stop) is unauthenticated on the network. "
            "Enable auth or bind to 127.0.0.1.",
            ui.host,
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
    """Register a DICOM handler for every enabled ``dicom`` destination.

    Handlers are registered *per destination* (``target_name``) — a type-only
    registry would collapse two enabled ``dicom`` destinations onto the last
    handler and misdeliver every study.
    """
    from mercure_gateway.audit import AuditLog

    forwarder = Forwarder(config, spool, audit=AuditLog(database))
    for destination in config.destinations:
        if destination.type == "dicom" and destination.enabled:
            forwarder.register_handler(
                "dicom", DICOMHandler(destination, spool), target_name=destination.name
            )
    return forwarder


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
    config: GatewayConfig, audit: Any
) -> tuple[dict[str, Any] | None, HubEventStreamer | None]:
    """Wire hub event streaming + registration when enabled; else ``(None, None)``.

    Returns a live hub-status dict (mutated by the background registration
    thread and read by the web admin panel) and the started streamer.  When hub
    reporting is disabled or under-configured, returns ``(None, None)``.
    """
    hub = config.audit.hub_reporting
    if not hub.enabled or not hub.bookkeeper_url:
        return None, None
    if not hub.api_key:
        logger.warning("audit.hub_reporting enabled but api_key is empty — hub reporting stays off")
        return None, None
    streamer = HubEventStreamer(hub.bookkeeper_url, hub.api_key, config.general.appliance_name)
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

    _warn_insecure(config)

    spool_dir = Path(config.storage.spool_dir)
    spool_dir.mkdir(parents=True, exist_ok=True)
    database = open_database(spool_dir / "mercure-gateway.db")
    from mercure_gateway.audit import AuditLog

    audit = AuditLog(database)

    # Hub reporting (S08): streams every audit event to the bookkeeper and
    # registers the gateway in the background — boot never blocks on the hub.
    hub_status, hub_streamer = _start_hub_reporting(config, audit)

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

    # Hot-unplug detection (USB mode only). The removal callback must stay
    # fail-safe: the device may vanish between the debounce probe and the
    # marker write (F10), so the marker write is best-effort and the
    # shutdown signal is always delivered.
    def _on_usb_removal() -> None:
        try:
            write_shutdown_marker(spool.spool_dir)
        except OSError:
            # Device already gone — nothing to flush, nothing to recover.
            logger.warning("could not write shutdown marker (device already removed)")
        receiver.stop()
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
    )
    disk_monitor.start()

    print(f"mercure-gateway {__version__}")
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
        spool.stop()
        # Graceful shutdown marker: its presence lets the next boot skip the
        # recovery scan (a crash/power-loss leaves no marker → scan runs).
        with contextlib.suppress(OSError):
            write_shutdown_marker(spool.spool_dir)
        database.close()

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
