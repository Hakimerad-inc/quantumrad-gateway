"""S08-T2 (RED): Event streaming to bookkeeper (PRD §8.2, US-10).

The gateway streams lifecycle events to the hub bookkeeper REST endpoint.
The critical invariant: any reporting failure (bookkeeper down, timeout, slow)
must never block the receive/forward pipeline — the ``feed()`` method returns
immediately regardless of bookkeeper state.

Event types: audit events (STUDY_RECEIVED, FORWARD_START, etc.) are forwarded
as-is, with gateway_name and timestamp metadata added by the streamer.
"""

from __future__ import annotations

import threading
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from mercure_gateway.hub_events import HubEventStreamer


def _wait_until(predicate: object, timeout: float = 5.0) -> None:
    """Spin until *predicate* is true — the worker runs on its own thread."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():  # type: ignore[operator]
            return
        time.sleep(0.02)
    raise AssertionError(f"condition not met within {timeout}s")


@pytest.fixture()
def streamer() -> HubEventStreamer:
    return HubEventStreamer(
        bookkeeper_url="https://hub.example.com",
        api_key="test-key",
        gateway_name="Gateway-A",
    )


# ══════════════════════════════════════════════════════════════════════
# Non-blocking invariant: feed() must never block
# ══════════════════════════════════════════════════════════════════════


def test_feed_returns_immediately_when_bookkeeper_down(streamer: HubEventStreamer) -> None:
    """feed() returns instantly when the bookkeeper connection fails."""
    streamer.start()

    start = time.monotonic()
    streamer.feed("STUDY_RECEIVED", {"study_uid": "1.2.3.4"})
    elapsed = time.monotonic() - start

    streamer.stop()
    assert elapsed < 0.5, "feed() blocked — isolation invariant violated"


def test_feed_returns_immediately_when_bookkeeper_slow(streamer: HubEventStreamer) -> None:
    """feed() returns instantly even when the bookkeeper is slow to respond."""
    streamer.start()

    start = time.monotonic()
    for _ in range(10):
        streamer.feed("FORWARD_START", {"study_uid": "1.2.3.4"})
    elapsed = time.monotonic() - start

    streamer.stop()
    assert elapsed < 0.5, "feed() blocked — isolation invariant violated"


def test_feed_returns_immediately_without_start(streamer: HubEventStreamer) -> None:
    """feed() is a no-op when the streamer has not been started."""
    start = time.monotonic()
    streamer.feed("STUDY_RECEIVED", {"study_uid": "1.2.3.4"})
    elapsed = time.monotonic() - start

    assert elapsed < 0.5


# ══════════════════════════════════════════════════════════════════════
# Queue behaviour
# ══════════════════════════════════════════════════════════════════════


def test_queue_holds_events_when_bookkeeper_down(streamer: HubEventStreamer) -> None:
    """Events are queued (not dropped) when the bookkeeper is unreachable."""
    streamer.start()

    streamer.feed("STUDY_RECEIVED", {"study_uid": "1.2.3.4"})
    streamer.feed("FORWARD_START", {"study_uid": "1.2.3.4"})

    # The queue should have 2 events (the background thread will retry)
    assert streamer.queue_size >= 2
    streamer.stop()


def test_queue_full_drops_oldest_event(streamer: HubEventStreamer) -> None:
    """When the queue is full, the oldest event is dropped (never block)."""
    streamer = HubEventStreamer(
        bookkeeper_url="https://hub.example.com",
        api_key="test-key",
        gateway_name="Gateway-A",
        max_queue_size=5,
    )
    streamer.start()

    for i in range(10):
        streamer.feed("STUDY_RECEIVED", {"idx": i})

    # Queue should be at most 5 (not 10) — oldest events dropped
    assert streamer.queue_size <= 5
    streamer.stop()


# ══════════════════════════════════════════════════════════════════════
# Disabled = no-op
# ══════════════════════════════════════════════════════════════════════


def test_disabled_does_not_queue(streamer: HubEventStreamer) -> None:
    """When enabled=False, feed() is a no-op and nothing is queued."""
    streamer = HubEventStreamer(
        bookkeeper_url="https://hub.example.com",
        api_key="test-key",
        gateway_name="Gateway-A",
        enabled=False,
    )
    # No need to start — disabled streamers are no-ops immediately
    streamer.feed("STUDY_RECEIVED", {"study_uid": "1.2.3.4"})
    assert streamer.queue_size == 0


# ══════════════════════════════════════════════════════════════════════
# Batching
# ══════════════════════════════════════════════════════════════════════


@patch("requests.post")
def test_events_batched_into_single_post(mock_post: MagicMock) -> None:
    """Multiple events are batched into a single POST to the bookkeeper."""
    resp = MagicMock()
    resp.ok = True
    resp.status_code = 200
    mock_post.return_value = resp

    streamer = HubEventStreamer(
        bookkeeper_url="https://hub.example.com",
        api_key="test-key",
        gateway_name="Gateway-A",
        max_batch_size=50,
    )
    streamer.start()

    streamer.feed("STUDY_RECEIVED", {"study_uid": "1.2.3.4"})
    streamer.feed("FORWARD_START", {"study_uid": "1.2.3.4"})
    streamer.feed("FORWARD_COMPLETE", {"study_uid": "1.2.3.4"})

    # Give the background thread time to drain the queue
    streamer.flush(timeout=2.0)
    streamer.stop()

    # The 3 events should be sent as a single batch
    assert mock_post.call_count >= 1
    last_call = mock_post.call_args
    body = last_call[1]["json"]
    assert len(body["events"]) == 3
    assert body["gateway"] == "Gateway-A"


# ══════════════════════════════════════════════════════════════════════
# Concurrent access
# ══════════════════════════════════════════════════════════════════════


def test_concurrent_feed_does_not_crash(streamer: HubEventStreamer) -> None:
    """Multiple threads can call feed() concurrently without crashing."""
    streamer.start()

    errors: list[Exception] = []

    def feed_many() -> None:
        for _ in range(100):
            try:
                streamer.feed("STUDY_RECEIVED", {"study_uid": "1.2.3.4"})
            except Exception as exc:
                errors.append(exc)

    threads = [threading.Thread(target=feed_many) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    streamer.stop()
    assert not errors, f"concurrent feed raised: {errors}"


# ══════════════════════════════════════════════════════════════════════
# Observability (review P0-10): a down bookkeeper must be *visible*
# ══════════════════════════════════════════════════════════════════════


@patch("requests.post")
def test_counters_reflect_a_permanently_down_bookkeeper(mock_post: MagicMock) -> None:
    """A bookkeeper that 401s forever raises failure/depth counters, not an error.

    ``is_running`` alone reports healthy here — it is thread liveness, and the
    worker thread is very much alive while it retries a hub that never answers.
    The counters are what an unmanned box's scraper has to alert on.
    """
    resp = MagicMock()
    resp.ok = False
    resp.status_code = 401
    resp.raise_for_status = MagicMock()
    mock_post.return_value = resp

    streamer = HubEventStreamer(
        bookkeeper_url="https://hub.example.com",
        api_key="test-key",
        gateway_name="Gateway-A",
        max_batch_size=5,
    )
    status: dict[str, Any] = {}
    streamer._status_dict = status  # noqa: SLF001 — simulate the composition root's wiring
    streamer.start()

    for _ in range(4):
        streamer.feed("STUDY_RECEIVED", {"study_uid": "1.2.3.4"})

    _wait_until(lambda: streamer.delivery_failures_total >= 4, timeout=5.0)

    assert streamer.delivery_failures_total >= 4
    assert streamer.queue_size > 0, "events must stay queued while the hub is down"
    assert streamer.delivered_total == 0
    # is_running is the misleading signal here: the worker is very much alive
    # while it retries a hub that never answers. The counters are the finding.
    assert streamer.is_running is True
    streamer.stop()


def test_counters_reflect_a_healthy_bookkeeper() -> None:
    """Delivered events are counted, and failures stay at zero."""
    resp = MagicMock()
    resp.ok = True
    resp.status_code = 200
    with patch("requests.post", return_value=resp):
        streamer = HubEventStreamer(
            bookkeeper_url="https://hub.example.com",
            api_key="test-key",
            gateway_name="Gateway-A",
        )
        streamer.start()
        for _ in range(3):
            streamer.feed("STUDY_RECEIVED", {"study_uid": "1.2.3.4"})
        streamer.flush(timeout=2.0)
        streamer.stop()

    assert streamer.delivered_total == 3
    assert streamer.delivery_failures_total == 0
    assert streamer.events_evicted_total == 0
    assert streamer.queue_size == 0


def test_evictions_are_counted_when_the_queue_overflows() -> None:
    """Overflow drops are visible — a silently dropping queue is a data loss."""
    streamer = HubEventStreamer(
        bookkeeper_url="https://hub.example.com",
        api_key="test-key",
        gateway_name="Gateway-A",
        max_queue_size=3,
    )
    streamer.start()  # no requests.post patch -> delivery fails, backlog grows
    try:
        for _ in range(10):
            streamer.feed("STUDY_RECEIVED", {"study_uid": "1.2.3.4"})
        # The bounded deque dropped 7 of the 10 events.
        assert streamer.events_evicted_total >= 7
    finally:
        streamer.stop()


def test_status_dict_tracks_worker_lifecycle() -> None:
    """'streaming' follows the worker, not a boot-time snapshot."""
    status: dict[str, Any] = {"streaming": False}
    streamer = HubEventStreamer(
        bookkeeper_url="https://hub.example.com",
        api_key="test-key",
        gateway_name="Gateway-A",
        status_dict=status,
    )
    assert status["streaming"] is False
    streamer.start()
    assert status["streaming"] is True
    streamer.stop()
    assert status["streaming"] is False
