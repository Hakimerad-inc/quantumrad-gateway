"""Seed an isolated E2E gateway: config, spool database, audit chain.

Run before the gateway process starts (from the Playwright globalSetup via
``uv run python e2e/seed.py``). Everything lands in a temp dir so the dev
box's real gateway (ports 8080/11115) is never touched.

Outputs the resolved paths as shell-friendly KEY=value lines on stdout.

Seeding uses the repo's own production code (Spool, AuditLog) — the same
store-and-forward path a real study takes — so the E2E specs exercise the
real API surface, not fixtures hand-crafted to match the UI.
"""

from __future__ import annotations

import hashlib
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from mercure_gateway.audit import AuditLog  # noqa: E402
from mercure_gateway.config import default_config  # noqa: E402
from mercure_gateway.spool import Spool  # noqa: E402
from mercure_gateway.spool.db import Database, open_database  # noqa: E402

# Auth: salted SHA-256 fallback hash (same scheme as tests/test_web_api.py),
# password "e2e-password" — bcrypt is not a default dependency.
PASSWORD = "e2e-password"
_SALT = "e2e0a1b2"
HASH = f"sha256${_SALT}${hashlib.sha256((_SALT + PASSWORD).encode()).hexdigest()}"

# Isolated ports (dev box runs the real stack on 8080/11114/11115).
WEB_PORT = 18299
RECEIVER_PORT = 18113


def build_config(base: Path):
    cfg = default_config()
    cfg.storage.spool_dir = str(base / "spool")
    cfg.web_ui.port = WEB_PORT
    cfg.web_ui.auth_enabled = True
    cfg.web_ui.auth_password_hash = HASH
    cfg.receiver.port = RECEIVER_PORT
    cfg.receiver.ae_title = "E2EGW"
    cfg.general.appliance_name = "E2E-Gateway"
    # Auto-enqueue is armed (no destinations → nothing to forward to, studies
    # stay RECEIVED and visible in the queue with zero routes).
    cfg.receiver.auto_enqueue_delay_sec = 2.0
    return cfg


def seed(base: Path) -> None:
    cfg = build_config(base)
    spool_dir = Path(cfg.storage.spool_dir)
    spool_dir.mkdir(parents=True, exist_ok=True)

    database = open_database(spool_dir / "mercure-gateway.db")
    audit = AuditLog(database)
    spool = Spool(database, cfg, audit=audit)

    # Study 1: plain RECEIVED study.
    spool.receive(
        "1.2.826.0.1.3680043.10.2001.1",
        accession="E2E-ACC-1",
        patient_name="E2E^Alpha",
        modality="CT",
        study_description="E2E seed study one",
        num_instances=1,
    )
    # Study 2: further along the pipeline so the queue shows state variety.
    spool.receive(
        "1.2.826.0.1.3680043.10.2001.2",
        accession="E2E-ACC-2",
        patient_name="E2E^Beta",
        modality="MR",
        study_description="E2E seed study two",
        num_instances=2,
    )
    # Study 3 with a FAILED-style route history is not seeded here: retrying
    # requires a real destination; the retry E2E covers the endpoint contract
    # (400 when there are no incomplete routes) instead.
    spool.stop()
    database.close()


def main() -> int:
    base = Path(tempfile.mkdtemp(prefix="qrad-e2e-"))
    seed(base)
    # Pre-generate the config file the gateway will load.
    from mercure_gateway.config import save_config

    cfg = build_config(base)
    config_path = base / "e2e-gateway.json"
    save_config(cfg, config_path)

    # Shell-friendly env for the Playwright global teardown + specs.
    lines = [
        f"E2E_DIR={base}",
        f"E2E_CONFIG={config_path}",
        f"E2E_WEB_PORT={WEB_PORT}",
        f"E2E_PASSWORD={PASSWORD}",
    ]
    env_file = base / "env.sh"
    env_file.write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
