"""Console entry point for mercure-gateway (scaffold).

Loads configuration, opens the spool database, starts the receiver + spool and
prints a status banner. The desktop shell (``ui.run_desktop``) is a placeholder
per PRD §5.1 / §13 Q1.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from mercure_gateway import __version__
from mercure_gateway.config import default_config, load_config, save_config
from mercure_gateway.receiver import Receiver
from mercure_gateway.spool import Spool
from mercure_gateway.spool.db import open_database

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
    return parser


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

    receiver = Receiver(config.receiver, spool)
    receiver.start()

    print(f"mercure-gateway {__version__}")
    print(f"  receiver  : AET={config.receiver.ae_title} port={config.receiver.port}")
    print(f"  spool     : {spool.spool_dir}")
    print(f"  queued    : {spool.queued_count()} studies awaiting delivery")

    receiver.stop()
    database.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
