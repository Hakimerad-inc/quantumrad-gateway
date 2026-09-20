"""Pipeline snapshot service + destination health monitor (web admin).

``pipeline_snapshot`` assembles the one-shot payload that drives the Pipeline
flow view: component health, queue histogram, inbound rate, and per-
destination route rollups with cached C-ECHO reachability.

``DestinationHealthMonitor`` probes each enabled ``dicom`` destination in a
daemon thread on a fixed cadence. Probes run in a small executor so one dark
PACS cannot delay the others, and results are cached — the HTTP request path
never blocks on network I/O (a dead PACS can hold an echo for seconds).

``RouteRollupCache`` applies the same idea to the per-destination rollup:
``count_routes_by_target`` is a GROUP BY over every routing row, and the flow
view polls for it every couple of seconds, so the aggregate is memoized for
a short window.
"""

from __future__ import annotations

import threading
import time
import weakref
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from mercure_gateway.web.echo import echo_destination

if TYPE_CHECKING:
    from mercure_gateway.config import GatewayConfig
    from mercure_gateway.spool import Spool
    from mercure_gateway.spool.db import Database

__all__ = ["DestinationHealthMonitor", "RouteRollupCache", "pipeline_snapshot"]

_PROBE_INTERVAL_SEC = 30.0
_PROBE_TIMEOUT_SEC = 3.0
# The flow view polls faster than this, so a memo at this TTL keeps the
# rollup fresh for the UI while collapsing the GROUP BY to one aggregate per
# window (see RouteRollupCache).
_ROLLUP_TTL_SEC = 5.0


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


class RouteRollupCache:
    """Short-TTL memo for the per-destination route rollup (pipeline view).

    ``count_routes_by_target`` groups all of ``task_routing`` — studies ×
    destinations — and the flow view asks for it every couple of seconds. The
    rollup only moves when a route transitions, so memoizing it for a few
    seconds takes the aggregate off the common request path; a dashboard a
    few seconds behind is the same tradeoff ``DestinationHealthMonitor``
    makes for C-ECHO probes.

    Same shape as that monitor: one lock-guarded store, populated under the
    lock so a burst of polls loads it once, and serving a cached entry never
    touches the database. Entries are per spool database
    (``_rollup_caches``), so two gateways in one process cannot see each
    other's rollups and a closed database takes its memo with it.
    """

    def __init__(self, *, ttl_sec: float = _ROLLUP_TTL_SEC) -> None:
        self._ttl_sec = ttl_sec
        self._lock = threading.Lock()
        self._rows: list[dict[str, Any]] | None = None
        self._expires_at = 0.0

    def get(self, loader: Callable[[], list[dict[str, Any]]]) -> list[dict[str, Any]]:
        """Return the rollup, loading it through *loader* if the TTL expired."""
        with self._lock:
            now = time.monotonic()
            if self._rows is not None and now < self._expires_at:
                return self._rows
            rows = loader()
            self._rows = rows
            self._expires_at = now + self._ttl_sec
            return rows

    def invalidate(self) -> None:
        """Drop the memo so the next ``get`` reloads."""
        with self._lock:
            self._rows = None


# One memo per spool database: keyed weakly so a closed/replaced database
# (tests build one per case) drops its entry instead of keeping it alive.
_rollup_caches: weakref.WeakKeyDictionary[Database, RouteRollupCache] = (
    weakref.WeakKeyDictionary()
)


def _rollup_cache(db: Database) -> RouteRollupCache:
    """The route-rollup memo belonging to *db* (created on first use)."""
    cache = _rollup_caches.get(db)
    if cache is None:
        cache = RouteRollupCache()
        _rollup_caches[db] = cache
    return cache


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
    # The rollup is a GROUP BY over all routing rows and the view polls for it
    # every few seconds — memoize it briefly (RouteRollupCache).
    routes = _rollup_cache(spool.database).get(spool.count_routes_by_target)

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
    # Counted in SQL against idx_studies_created_at: materializing the whole
    # studies ⋈ task_routing list to count it in Python scaled with the
    # table, and this endpoint is polled constantly.
    received_hour = spool.database.count_studies_since(one_hour_ago)

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
