"""TDD (S10-T7, RED): Disk full management — 90 % capacity warning + web UI.

Behaviors:
1. ``GET /api/system/disk`` reports live spool capacity (``usage_pct``,
   ``total/used/free_bytes``) plus the warning threshold and the
   ``purge_on_disk_full`` flag from config (usb-dongle-spec §5.3).
2. ``over_threshold`` flips once usage crosses ``warning_pct`` (default 90%).
3. A measurement failure surfaces as 503 — the SPA must never show "OK" when
   the filesystem cannot be measured.
4. With the USB profile applied (S10-T5) the endpoint reports the aggressive
   ``purge_on_disk_full`` + 90 % threshold. (Oldest-first purge of DELIVERED,
   undelivered retained, is covered by ``test_disk_monitor.py`` and the
   ``tests/chaos/test_disk_full.py`` scenario.)
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from mercure_gateway.config import apply_usb_defaults, default_config
from mercure_gateway.spool import Spool
from mercure_gateway.spool.db import mem_database
from mercure_gateway.web import create_app


def usage(pct: float) -> SimpleNamespace:
    return SimpleNamespace(
        total=100_000_000_000, used=pct * 1_000_000_000, free=100_000_000_000
    )


@pytest.fixture()
def spool() -> Spool:
    return Spool(mem_database())


def make_client(spool: Spool, monkeypatch: pytest.MonkeyPatch, *, pct: float) -> TestClient:
    cfg = default_config()
    application = create_app(cfg, spool)
    monkeypatch.setattr(
        "mercure_gateway.web.routes.shutil.disk_usage", lambda _p: usage(pct)
    )
    return TestClient(application)


# ── Test 1: capacity + threshold reported ────────────────────────────────


def test_disk_endpoint_reports_capacity_and_threshold(
    spool: Spool, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(spool, monkeypatch, pct=95.0)

    data = client.get("/api/system/disk").json()

    assert data["usage_pct"] == 95.0
    assert data["total_bytes"] == 100_000_000_000
    assert data["used_bytes"] == 95_000_000_000
    assert data["free_bytes"] == 100_000_000_000
    assert data["warning_pct"] == 90  # storage.disk_full_warning_pct default
    assert data["over_threshold"] is True
    assert data["purge_on_disk_full"] is False  # disabled by default


# ── Test 2: below threshold clears the warning state ─────────────────────


def test_disk_endpoint_below_threshold_not_over(
    spool: Spool, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(spool, monkeypatch, pct=50.0)

    data = client.get("/api/system/disk").json()

    assert data["usage_pct"] == 50.0
    assert data["over_threshold"] is False


# ── Test 3: measurement failure is a 503, never a false "ok" ─────────────


def test_disk_endpoint_fails_cleanly_when_fs_unavailable(
    spool: Spool, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(_p: str) -> SimpleNamespace:
        raise OSError("stale NFS handle")

    cfg = default_config()
    application = create_app(cfg, spool)
    monkeypatch.setattr("mercure_gateway.web.routes.shutil.disk_usage", _boom)

    response = TestClient(application).get("/api/system/disk")

    assert response.status_code == 503


# ── Test 4: USB profile surfaces aggressive purge settings ───────────────


def test_disk_endpoint_reflects_usb_profile(
    spool: Spool, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = default_config()
    cfg.usb_mode.enabled = True
    apply_usb_defaults(cfg)
    application = create_app(cfg, spool)
    monkeypatch.setattr(
        "mercure_gateway.web.routes.shutil.disk_usage", lambda _p: usage(95.0)
    )

    data = TestClient(application).get("/api/system/disk").json()

    assert data["warning_pct"] == 90
    assert data["purge_on_disk_full"] is True  # S10-T5 profile applied
    assert data["over_threshold"] is True