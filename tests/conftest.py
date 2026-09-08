"""Shared test fixtures for the mercure-gateway test suite.

Provides reusable fakes for the Receiver and Forwarder that satisfy the
protocol contracts without real DICOM/network I/O.
"""

from __future__ import annotations

import pytest

from mercure_gateway.config import DICOMDestination, GatewayConfig, default_config


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
