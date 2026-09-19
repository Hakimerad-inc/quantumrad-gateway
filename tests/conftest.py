"""Shared test fixtures for the mercure-gateway test suite.

Provides reusable fakes for the Receiver and Forwarder that satisfy the
protocol contracts without real DICOM/network I/O.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mercure_gateway.config import DICOMDestination, GatewayConfig, default_config


def pytest_configure(config: pytest.Config) -> None:
    """Enable coverage collection in spawned subprocesses (P1-6).

    ``tests/test_main.py`` exercises the composition root by launching
    ``python -m mercure_gateway.main`` for real. The venv's ``a1_coverage.pth``
    (pytest-cov) already calls ``coverage.process_startup()`` in *every*
    interpreter at startup — but that call is a no-op unless
    ``COVERAGE_PROCESS_START`` names a config file. Setting it here is what
    makes the child's lines land in the report.

    This runs at configure time, not as a fixture, because it must be live in
    the environment *before* any child spawns. It is gated on ``--cov`` so a
    plain ``pytest`` run is unaffected, and the path is resolved absolutely
    from ``config.rootpath`` so a child whose cwd differs still finds it.
    """
    # pytest-cov registers --cov with dest='cov_source' (bare --cov stores
    # True, --cov=pkg stores the package name, absent stores []).
    if config.getoption("cov_source"):
        os.environ["COVERAGE_PROCESS_START"] = str(
            (Path(config.rootpath) / "pyproject.toml").resolve(),
        )


@pytest.fixture(autouse=True)
def _plaintext_secrets_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests opt out of at-rest encryption unless they are testing it.

    A default install now generates a config master password on first boot
    (review P1-1) and persists it — to the OS keyring on a desktop, or a 0600
    sidecar file on a headless box. Neither belongs in an unrelated unit test,
    so the suite takes the documented opt-out. test_config_encryption.py
    delenv's this where it needs the real behaviour.
    """
    monkeypatch.setenv("MERCURE_GATEWAY_ALLOW_PLAINTEXT_SECRETS", "1")


class FakeReceiver:
    """In-memory receiver stub for tests."""

    def __init__(self) -> None:
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self) -> None:
        self._running = True

    def stop(self) -> None:
        self._running = False


class FakeForwarder:
    """In-memory forwarder stub for tests."""

    def __init__(self) -> None:
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self) -> None:
        self._running = True

    def stop(self) -> None:
        self._running = False


@pytest.fixture()
def fake_receiver() -> FakeReceiver:
    return FakeReceiver()


@pytest.fixture()
def fake_forwarder() -> FakeForwarder:
    return FakeForwarder()


@pytest.fixture()
def default_gateway_config() -> GatewayConfig:
    return default_config()


@pytest.fixture()
def target_hub() -> DICOMDestination:
    return DICOMDestination(
        name="hub", type="dicom", host="hub.local", port=11112, aet_target="MERCURE"
    )


@pytest.fixture()
def target_pacs() -> DICOMDestination:
    return DICOMDestination(
        name="pacs", type="dicom", host="pacs.local", port=104, aet_target="PACS"
    )
