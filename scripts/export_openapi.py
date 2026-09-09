#!/usr/bin/env python
"""Export the web API's OpenAPI schema (review M8 follow-up).

The schema is the source of truth for the SPA's generated TypeScript types
(``web/src/types/api-schema.ts``). Usage:

    uv run python scripts/export_openapi.py > /tmp/openapi.json
    cd web && npx openapi-typescript /tmp/openapi.json -o src/types/api-schema.ts
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mercure_gateway.config import default_config  # noqa: E402
from mercure_gateway.spool import Spool  # noqa: E402
from mercure_gateway.spool.db import mem_database  # noqa: E402
from mercure_gateway.web import create_app  # noqa: E402


def main() -> None:
    cfg = default_config()
    app = create_app(cfg, Spool(mem_database()))
    json.dump(app.openapi(), sys.stdout, indent=2)
    print()


if __name__ == "__main__":
    main()
