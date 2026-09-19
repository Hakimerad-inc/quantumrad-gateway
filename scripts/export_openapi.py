#!/usr/bin/env python
"""Export the web API's OpenAPI schema (review M8 follow-up).

The schema is the source of truth for the SPA's generated TypeScript types
(``web/src/types/api-schema.ts``). Usage:

    just gen-api

or, equivalently:

    uv run python scripts/export_openapi.py --output ../mercure-gateway.openapi.json
    cd web && npx openapi-typescript ../mercure-gateway.openapi.json -o src/types/api-schema.ts

Writes to stdout when no ``--output`` is given, for piping into other tools.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mercure_gateway.config import default_config  # noqa: E402
from mercure_gateway.spool import Spool  # noqa: E402
from mercure_gateway.spool.db import mem_database  # noqa: E402
from mercure_gateway.web import create_app  # noqa: E402

_SCHEMA_NAME = "mercure-gateway.openapi.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        "-o",
        nargs="?",
        const=_SCHEMA_NAME,
        default=None,
        help=f"write the schema here (default: {_SCHEMA_NAME} when the flag is "
        "given with no value; stdout when the flag is absent)",
    )
    args = parser.parse_args(argv)

    cfg = default_config()
    app = create_app(cfg, Spool(mem_database()))
    document = json.dumps(app.openapi(), indent=2) + "\n"

    if args.output is None:
        sys.stdout.write(document)
        return 0

    # Default to the repo root (beside the justfile) so the recipe works from
    # any cwd — the Tauri/PyInstaller builds are invoked from deeper dirs.
    here = Path(__file__).resolve().parents[1]
    out = Path(args.output)
    if not out.is_absolute():
        out = here / out
    out.write_text(document, encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
