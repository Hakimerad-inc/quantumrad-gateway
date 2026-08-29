"""Console entry point for mercure-gateway.

Loads configuration, opens the spool database, starts the receiver + spool and
optionally launches the web admin panel (FastAPI on localhost:8080).  The desktop
shell (``ui.run_desktop``) is a placeholder per PRD §5.1 / §13 Q1.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from mercure_gateway import __version__
from mercure_gateway.config import GatewayConfig, default_config, load_config, save_config
from mercure_gateway.hotplug import HotplugDetector, write_shutdown_marker
from mercure_gateway.receiver import Receiver
from mercure_gateway.recovery import recover
from mercure_gateway.spool import Spool
from mercure_gateway.spool.db import Database, open_database

__all__ = ["main"]


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


def _run_web_admin(config: GatewayConfig, spool: Spool, port: int) -> None:
    """Start the FastAPI web admin panel (blocking)."""
    import uvicorn

    from mercure_gateway.web import create_app

    app = create_app(config, spool)
    host = config.web_ui.host
    print(f"  web admin : http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="info")


def main(argv: list[str] | None = None) -> int:
    """Run the gateway console entry point; returns a process exit code."""
    args = _build_parser().parse_args(argv)

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

    spool_dir = Path(config.storage.spool_dir)
    spool_dir.mkdir(parents=True, exist_ok=True)
    database = open_database(spool_dir / "mercure-gateway.db")
    spool = Spool(database, config)

    # Recovery scan — reconcile spool files with DB on startup
    recovery_result = recover(spool)
    if recovery_result.had_marker or recovery_result.studies_recovered or recovery_result.files_without_db:
        print(f"  recovery  : {recovery_result.studies_recovered} recovered, "
              f"{recovery_result.files_without_db} orphaned files registered")

    receiver = Receiver(config.receiver, spool)
    receiver.start()

    # Hot-unplug detection (USB mode only)
    def _on_usb_removal() -> None:
        receiver.stop()
        write_shutdown_marker(spool.spool_dir)
        print("\nUSB device removed — gateway shut down safely")

    hotplug = HotplugDetector(
        spool.spool_dir,
        _on_usb_removal,
        enabled=config.usb_mode.enabled and config.usb_mode.hot_unplug_safe,
    )
    hotplug.start()

    print(f"mercure-gateway {__version__}")
    print(f"  receiver  : AET={config.receiver.ae_title} port={config.receiver.port}")
    print(f"  spool     : {spool.spool_dir}")
    print(f"  queued    : {spool.queued_count()} studies awaiting delivery")

    if args.web:
        web_port = args.port or config.web_ui.port
        try:
            _run_web_admin(config, spool, web_port)
        except KeyboardInterrupt:
            print("\nShutting down web admin...")
        finally:
            hotplug.stop()
            receiver.stop()
            database.close()
        return 0

    hotplug.stop()
    receiver.stop()
    database.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
