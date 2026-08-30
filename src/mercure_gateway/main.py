"""Console entry point for mercure-gateway.

Loads configuration, opens the spool database, wires the store-and-forward
pipeline (receiver → spool → forwarder with DICOM handler per destination)
and optionally launches the web admin panel (FastAPI on localhost:8080).
The desktop shell is the Tauri wrapper per ADR-0002.

Composition root: this module owns construction and shutdown ordering of all
components — receiver, forwarder, hotplug detector, web admin, database.
Shutdown order is the reverse of construction: web → hotplug → forwarder →
receiver → database close.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import sys
import threading
from pathlib import Path
from typing import Any

from mercure_gateway import __version__
from mercure_gateway.config import GatewayConfig, default_config, load_config, save_config
from mercure_gateway.forwarder import Forwarder
from mercure_gateway.forwarder.handlers.dicom import DICOMHandler
from mercure_gateway.hotplug import HotplugDetector, write_shutdown_marker
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


def _build_hub_status(config: GatewayConfig) -> dict[str, Any] | None:
    """Build a hub-status snapshot for the web admin panel (S08-T8).

    Returns ``None`` when hub reporting is not enabled, so the UI renders
    ``—`` instead of a misleading "inactive".
    """
    hub = config.audit.hub_reporting
    if not hub.enabled or not hub.bookkeeper_url:
        return None
    return {
        "registered": False,
        "streaming": False,
        "bookkeeper_url": hub.bookkeeper_url,
    }


def _run_web_admin(
    config: GatewayConfig,
    spool: Spool,
    receiver: Receiver,
    forwarder: Forwarder,
    report_retriever: Any,
    port: int,
    text_log: TextLog | None = None,
    config_path: Path | None = None,
) -> None:
    """Start the FastAPI web admin panel (blocking)."""
    import uvicorn

    from mercure_gateway.web import create_app

    app = create_app(config, spool, config_path=config_path)
    app.state.receiver = receiver
    app.state.forwarder = forwarder
    app.state.report_retriever = report_retriever
    app.state.hub_status = _build_hub_status(config)
    if text_log is not None:
        app.state.text_log_path = str(text_log._path)
    host = config.web_ui.host
    print(f"  web admin : http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="info")


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

    _warn_insecure(config)

    spool_dir = Path(config.storage.spool_dir)
    spool_dir.mkdir(parents=True, exist_ok=True)
    database = open_database(spool_dir / "mercure-gateway.db")
    from mercure_gateway.audit import AuditLog

    audit = AuditLog(database)

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

    print(f"mercure-gateway {__version__}")
    print(f"  receiver  : AET={config.receiver.ae_title} port={config.receiver.port}")
    print(f"  forwarder : {forwarder.is_running and 'running' or 'stopped'}")
    print(f"  spool     : {spool.spool_dir}")
    print(f"  queued    : {spool.queued_count()} studies awaiting delivery")

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
                )
            except KeyboardInterrupt:
                print("\nShutting down web admin...")
        else:
            # Headless mode: run until interrupt or USB removal.
            with contextlib.suppress(KeyboardInterrupt):
                shutdown_done.wait()
    finally:
        hotplug.stop()
        forwarder.stop()
        receiver.stop()
        report_retriever.stop()
        spool.stop()
        # Graceful shutdown marker: its presence lets the next boot skip the
        # recovery scan (a crash/power-loss leaves no marker → scan runs).
        with contextlib.suppress(OSError):
            write_shutdown_marker(spool.spool_dir)
        database.close()

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
