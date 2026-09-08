"""Pipeline snapshot service + destination health monitor (web admin).

``pipeline_snapshot`` assembles the one-shot payload that drives the Pipeline
flow view: component health, queue histogram, inbound rate, and per-
destination route rollups with cached C-ECHO reachability.

``DestinationHealthMonitor`` probes each enabled ``dicom`` destination in a
daemon thread on a fixed cadence. Probes run in a small executor so one dark
PACS cannot delay the others, and results are cached — the HTTP request path
never blocks on network I/O (a dead PACS can hold an echo for seconds).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from mercure_gateway.web.echo import echo_destination

if TYPE_CHECKING:
    from mercure_gateway.config import GatewayConfig
    from mercure_gateway.spool import Spool

__all__ = ["DestinationHealthMonitor", "pipeline_snapshot"]

_PROBE_INTERVAL_SEC = 30.0
_PROBE_TIMEOUT_SEC = 3.0


class DestinationHealthMonitor:
    """Background C-ECHO prober with a thread-safe result cache.

    Cache entry per destination name::

        {"status": "ok"|"refused"|"timeout"|"error"|"n/a",
         "checked_at": iso8601, "latency_ms": int}
    """

    def __init__(
        self,
        config: GatewayConfig,
        *,
        interval_sec: float = _PROBE_INTERVAL_SEC,
    ) -> None:
        self._config = config
        self._interval_sec = interval_sec
        self._cache: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="destination-health", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # -- access ------------------------------------------------------------

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """Return a copy of the current health cache (never blocks on I/O)."""
        with self._lock:
            return {name: dict(entry) for name, entry in self._cache.items()}

    # -- worker ------------------------------------------------------------

    def _run(self) -> None:
        # Probe immediately on start so the UI has data without waiting a full
        # interval, then settle into the fixed cadence.
        while not self._stop.is_set():
            self._probe_all()
            self._stop.wait(self._interval_sec)

    def _probe_all(self) -> None:
        targets = [
            dest for dest in self._config.destinations
            if dest.enabled and dest.type == "dicom"
        ]
        if not targets:
            return
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {dest.name: pool.submit(self._probe_one, dest) for dest in targets}
            results = {name: future.result() for name, future in futures.items()}
        with self._lock:
            self._cache.update(results)

    @staticmethod
    def _probe_one(dest: Any) -> dict[str, Any]:
        started = time.monotonic()
        try:
            status = echo_destination(dest, timeout_sec=_PROBE_TIMEOUT_SEC)
        except Exception:  # noqa: BLE001 — a probe must never kill the thread
            status = "error"
        latency_ms = int((time.monotonic() - started) * 1000)
        return {
            "status": status,
            "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "latency_ms": latency_ms,
        }


def pipeline_snapshot(
    config: GatewayConfig,
    spool: Spool,
    health: Mapping[str, dict[str, Any]] | None,
    *,
    receiver_running: bool | None = None,
    forwarder_running: bool | None = None,
    reports_running: bool | None = None,
) -> dict[str, Any]:
    """Assemble the pipeline flow payload (see PipelineView).

    Component flags default to the running objects on *spool*'s owning app
    when not supplied explicitly (tests pass them directly).
    """
    queue = spool.count_states()
    routes = spool.count_routes_by_target()

    rollup: dict[str, dict[str, Any]] = {}
    for row in routes:
        entry = rollup.setdefault(
            row["target_name"],
            {"type": row["target_type"], "routes": {}},
        )
        entry["routes"][row["status"]] = int(row["n"])
        entry["last_activity"] = row["last_activity"]

    one_hour_ago = (
        datetime.now(UTC) - timedelta(hours=1)
    ).strftime("%Y-%m-%d %H:%M:%S")
    received_hour = sum(
        1 for row in spool.list_studies_with_route_counts()
        if row["created_at"] >= one_hour_ago
    )

    destinations: list[dict[str, Any]] = []
    for dest in config.destinations:
        if not dest.enabled:
            continue
        info = rollup.get(dest.name, {"routes": {}})
        dest_view: dict[str, Any] = {
            "name": dest.name,
            "type": dest.type,
            "routes": {
                "complete": info["routes"].get("complete", 0),
                "sending": info["routes"].get("sending", 0),
                "waiting": info["routes"].get("waiting", 0),
                "error": info["routes"].get("error", 0),
            },
            "last_activity": info.get("last_activity"),
        }
        if dest.type == "dicom":
            dest_view["host"] = dest.host
            dest_view["port"] = dest.port
            dest_view["aet"] = dest.aet_target
            dest_view["health"] = (health or {}).get(dest.name)
        destinations.append(dest_view)

    return {
        "components": {
            "receiver": receiver_running,
            "forwarder": forwarder_running,
            "reports": reports_running,
        },
        "queue": {
            "total": queue.get("RECEIVING", 0) + queue.get("RECEIVED", 0)
            + queue.get("QUEUED", 0) + queue.get("SENDING", 0)
            + queue.get("SENT", 0) + queue.get("ERROR", 0) + queue.get("FAILED", 0),
            "queued": queue.get("QUEUED", 0) + queue.get("RECEIVED", 0),
            "sending": queue.get("SENDING", 0),
            "sent": queue.get("SENT", 0),
            "error": queue.get("ERROR", 0),
            "failed": queue.get("FAILED", 0),
        },
        "receiver_counts": {"received_last_hour": received_hour},
        "destinations": destinations,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
