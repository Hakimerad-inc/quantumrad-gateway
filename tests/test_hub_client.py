"""S08-T1 (RED): Hub registration client (PRD §8.2, US-10).

On first run when configured, the gateway registers itself with the mercure
hub via ``POST /register-gateway``.  Behaviors:

1. Registration payload carries name/version/contact
2. A 5xx response triggers a retry
3. ``enabled=False`` is a no-op (no HTTP call)
4. A 2xx response marks registration successful
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from mercure_gateway.hub_client import HubClient


@pytest.fixture()
def hub() -> HubClient:
    return HubClient(
        bookkeeper_url="https://hub.example.com",
        api_key="test-key",
        gateway_name="Gateway-A",
        version="0.1.0",
        contact="admin@example.com",
    )


# ══════════════════════════════════════════════════════════════════════
# Registration payload
# ══════════════════════════════════════════════════════════════════════

@patch("requests.post")
def test_registration_payload(mock_post, hub: HubClient) -> None:
    resp = MagicMock()
    resp.ok = True
    resp.status_code = 200
    mock_post.return_value = resp

    result = hub.register()

    assert result.ok is True
    payload = mock_post.call_args[1]["json"]
    assert payload["name"] == "Gateway-A"
    assert payload["version"] == "0.1.0"
    assert payload["contact"] == "admin@example.com"
    assert mock_post.call_args[1]["headers"]["Authorization"] == "Token test-key"  # TD-19


# ══════════════════════════════════════════════════════════════════════
# Retry on 5xx
# ══════════════════════════════════════════════════════════════════════

@patch("requests.post")
def test_retries_on_5xx(mock_post, hub: HubClient) -> None:
    resp_500 = MagicMock()
    resp_500.ok = False
    resp_500.status_code = 500
    resp_ok = MagicMock()
    resp_ok.ok = True
    resp_ok.status_code = 200
    mock_post.side_effect = [resp_500, resp_500, resp_ok]

    result = hub.register()

    assert result.ok is True
    assert mock_post.call_count == 3


@patch("requests.post")
def test_fails_after_max_retries(mock_post, hub: HubClient) -> None:
    resp = MagicMock()
    resp.ok = False
    resp.status_code = 503
    mock_post.return_value = resp

    result = hub.register()

    assert result.ok is False
    assert mock_post.call_count == hub.max_retries


# ══════════════════════════════════════════════════════════════════════
# Disabled = no-op
# ══════════════════════════════════════════════════════════════════════

@patch("requests.post")
def test_disabled_is_noop(mock_post) -> None:
    hub = HubClient(
        bookkeeper_url="https://hub.example.com",
        api_key="test-key",
        gateway_name="Gateway-A",
        version="0.1.0",
        enabled=False,
    )
    result = hub.register()
    assert result.ok is True
    assert result.skipped is True
    mock_post.assert_not_called()


# ══════════════════════════════════════════════════════════════════════
# Non-5xx failure
# ══════════════════════════════════════════════════════════════════════

@patch("requests.post")
def test_non_5xx_failure_no_retry(mock_post, hub: HubClient) -> None:
    resp = MagicMock()
    resp.ok = False
    resp.status_code = 400
    mock_post.return_value = resp

    result = hub.register()

    assert result.ok is False
    assert mock_post.call_count == 1


@patch("requests.post")
def test_connection_error_fails(mock_post, hub: HubClient) -> None:
    mock_post.side_effect = ConnectionError("hub unreachable")

    result = hub.register()

    assert result.ok is False
