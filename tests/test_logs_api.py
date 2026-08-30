"""S06-T4 (RED): Web admin logs endpoint (product refinement §7.6).

The admin panel needs a dedicated operations-log viewer.  ``GET /api/logs``
returns the tail of the rotating text log (configurable limit, default 100)
with a ``total_available`` field so the UI can show "showing last N of M
lines".

Behaviors:
1. ``GET /api/logs`` returns the log tail as a list of strings
2. ``limit`` is honored (truncates from the front)
3. ``total_available`` reports how many lines exist
4. an empty/missing log file returns empty list + total 0
"""

from __future__ import annotations

from conftest import FakeForwarder, FakeReceiver
from fastapi.testclient import TestClient

from mercure_gateway.config import default_config
from mercure_gateway.spool import Spool
from mercure_gateway.spool.db import mem_database
from mercure_gateway.web import create_app


def test_logs_returns_tail() -> None:
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as td:
        lines = [f"line {i}" for i in range(10)]
        Path(td, "operations.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
        spool = Spool(mem_database())
        cfg = default_config()
        app = create_app(cfg, spool)
        app.state.receiver = FakeReceiver()
        app.state.forwarder = FakeForwarder()
        app.state.text_log_path = str(Path(td, "operations.log"))
        client = TestClient(app)
        r = client.get("/api/logs")
        assert r.status_code == 200
        data = r.json()
        assert data["lines"] == lines
        assert data["total_available"] == 10


def test_logs_honors_limit() -> None:
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as td:
        lines = [f"line {i}" for i in range(10)]
        Path(td, "operations.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
        spool = Spool(mem_database())
        cfg = default_config()
        app = create_app(cfg, spool)
        app.state.receiver = FakeReceiver()
        app.state.forwarder = FakeForwarder()
        app.state.text_log_path = str(Path(td, "operations.log"))
        client = TestClient(app)
        r = client.get("/api/logs?limit=3")
        assert r.status_code == 200
        data = r.json()
        assert data["lines"] == lines[-3:]
        assert data["total_available"] == 10


def test_logs_empty_file() -> None:
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as td:
        Path(td, "operations.log").write_text("", encoding="utf-8")
        spool = Spool(mem_database())
        cfg = default_config()
        app = create_app(cfg, spool)
        app.state.receiver = FakeReceiver()
        app.state.forwarder = FakeForwarder()
        app.state.text_log_path = str(Path(td, "operations.log"))
        client = TestClient(app)
        r = client.get("/api/logs")
        assert r.status_code == 200
        data = r.json()
        assert data["lines"] == []
        assert data["total_available"] == 0


def test_logs_missing_file() -> None:
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as td:
        spool = Spool(mem_database())
        cfg = default_config()
        app = create_app(cfg, spool)
        app.state.receiver = FakeReceiver()
        app.state.forwarder = FakeForwarder()
        app.state.text_log_path = str(Path(td, "missing.log"))
        client = TestClient(app)
        r = client.get("/api/logs")
        assert r.status_code == 200
        data = r.json()
        assert data["lines"] == []
        assert data["total_available"] == 0
