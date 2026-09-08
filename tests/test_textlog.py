"""S04-T3 (RED): rotating text log (PRD §2.3, §7 — dual logging).

A file-based operations log runs alongside the SQLite audit chain.  It rotates
when the active file exceeds a size threshold, keeps a stable line format
(timestamp, level, message) and never writes PHI beyond the configured scope.
"""

from __future__ import annotations

import re
from pathlib import Path

from mercure_gateway.textlog import TextLog


def test_writes_timestamped_lines(tmp_path: Path) -> None:
    log = TextLog(tmp_path / "gateway.log")
    log.info("receiver started")
    log.error("forward failed: connection refused")

    lines = (tmp_path / "gateway.log").read_text().strip().splitlines()
    assert len(lines) == 2
    ts = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    assert re.match(rf"^{ts} INFO  receiver started$", lines[0])
    assert re.match(rf"^{ts} ERROR forward failed: connection refused$", lines[1])


def test_rotates_at_size_threshold(tmp_path: Path) -> None:
    log = TextLog(tmp_path / "gateway.log", max_bytes=200, max_backups=1)
    # Write enough to exceed the threshold across several calls.
    for _ in range(20):
        log.info("0123456789" * 10)  # ~110 bytes each → rotation after a few

    active = tmp_path / "gateway.log"
    backup = tmp_path / "gateway.log.1"
    assert active.exists()
    assert backup.exists(), "expected at least one rotated backup"
    assert active.stat().st_size <= 200, "active file must stay under the threshold"


def test_rotation_limits_backup_count(tmp_path: Path) -> None:
    log = TextLog(tmp_path / "gateway.log", max_bytes=100, max_backups=2)
    for _ in range(50):
        log.info("0123456789" * 10)

    backups = sorted(p.name for p in tmp_path.glob("gateway.log.*"))
    assert len(backups) <= 2


def test_log_is_append_only(tmp_path: Path) -> None:
    log = TextLog(tmp_path / "gateway.log")
    log.info("first")
    log.info("second")
    log.info("third")

    content = (tmp_path / "gateway.log").read_text()
    assert content.count("first") == 1
    assert content.count("second") == 1
    assert content.count("third") == 1


def test_phi_scoping_omits_patient_fields_by_default(tmp_path: Path) -> None:
    """The operations log must not include patient fields unless phi_scope=full."""
    log = TextLog(tmp_path / "gateway.log")
    log.info(
        "study received",
        phi={"study_uid": "1.2.3.4", "patient_name": "DOE^JOHN", "mrn": "12345"},
    )

    content = (tmp_path / "gateway.log").read_text()
    assert "DOE^JOHN" not in content
    assert "12345" not in content


def test_phi_full_scope_includes_patient_fields(tmp_path: Path) -> None:
    log = TextLog(tmp_path / "gateway.log", phi_scope="full")
    log.info(
        "study received",
        phi={"study_uid": "1.2.3.4", "patient_name": "DOE^JOHN", "mrn": "12345"},
    )

    content = (tmp_path / "gateway.log").read_text()
    assert "DOE^JOHN" in content


def test_missing_log_dir_is_created(tmp_path: Path) -> None:
    log = TextLog(tmp_path / "logs" / "gateway.log")
    log.info("hello")
    assert (tmp_path / "logs" / "gateway.log").exists()
