"""Store-and-forward spool: SQLite queue + DICOM file storage.

Implements the study lifecycle state machine from PRD §3.3:

    RECEIVING → RECEIVED → QUEUED → SENDING → SENT (per destination)
                                  │
                                  └→ ERROR → (retry) → ... → FAILED

Filesystem storage of DICOM blobs is functional (``store_instance`` writes
received instances); the state transitions and the concurrency-safe
"claim next task" are fully implemented.

Security note:
- Received UIDs are validated (:func:`validate_uid`) before they reach the
  filesystem or the database — DICOM allows arbitrary strings in UID fields,
  and unvalidated values would permit path traversal via C-STORE
  (``spool/{study_uid}/{series_uid}/{instance_uid}.dcm``).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import threading
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from mercure_gateway.audit import AuditLog
from mercure_gateway.audit.events import (
    RETRY_MANUAL,
    STUDY_FAILED,
    STUDY_QUEUED,
    STUDY_RECEIVED,
    STUDY_SENT,
)
from mercure_gateway.config import Destination, GatewayConfig
from mercure_gateway.rules import RuleEngine, RuleSyntaxError
from mercure_gateway.spool.db import Database

logger = logging.getLogger(__name__)

__all__ = [
    "ClaimedTask",
    "InvalidUIDError",
    "Spool",
    "StudyState",
    "RouteStatus",
    "validate_uid",
]

# DICOM UID: digits and dots only, no leading/trailing dot, max 64 chars.
_UID_RE = re.compile(r"^[0-9]+(\.[0-9]+)*$")


class InvalidUIDError(ValueError):
    """Raised when a received DICOM UID fails validation."""


def validate_uid(uid: str, *, what: str = "UID") -> str:
    """Validate a DICOM UID string and return it.

    Raises :class:`InvalidUIDError` for values that could escape the spool
    directory (``..``, separators, empty, over 64 chars).
    """
    if not uid or len(uid) > 64 or not _UID_RE.match(uid):
        raise InvalidUIDError(f"invalid DICOM {what}: {uid!r}")
    return uid


def _tag(dataset: Any, name: str) -> str | None:
    """Extract a DICOM tag as a string, or ``None`` when absent."""
    value = getattr(dataset, name, None)
    if value is None:
        return None
    return str(value)


class StudyState(StrEnum):
    """Study-level lifecycle states (PRD §3.3 / §5.4)."""

    RECEIVING = "RECEIVING"
    RECEIVED = "RECEIVED"
    QUEUED = "QUEUED"
    SENDING = "SENDING"
    SENT = "SENT"
    ERROR = "ERROR"
    FAILED = "FAILED"


class RouteStatus(StrEnum):
    """Per-destination routing statuses (PRD §5.4)."""

    WAITING = "waiting"
    SENDING = "sending"
    COMPLETE = "complete"
    ERROR = "error"


@dataclass(frozen=True)
class ClaimedTask:
    """A task claimed from the queue by a forwarding worker."""

    route_id: int
    study_id: int
    target_name: str
    target_type: str


class Spool:
    """High-level store-and-forward queue backed by the SQLite :class:`Database`."""

    def __init__(
        self,
        database: Database,
        config: GatewayConfig | None = None,
        *,
        audit: AuditLog | None = None,
        spool_dir: Path | str | None = None,
    ) -> None:
        self._db = database
        self._config = config
        self._audit = audit
        if spool_dir is not None:
            # Public test/embedder override; takes precedence over the
            # config-derived path so drills never poke _spool_dir directly.
            self._spool_dir = Path(spool_dir)
        elif config is not None:
            self._spool_dir = Path(config.storage.spool_dir)
        else:
            self._spool_dir = Path("spool")
        # Auto-enqueue scheduler (US-03): one pending timer *per study*, armed
        # on each store_instance and fired after the configured idle delay.
        # NB: this was a single Spool-wide timer slot — a second study's
        # arrival cancelled the first study's pending enqueue, stranding all
        # but the last study of a burst in RECEIVED (E1 dry-run, 2026-09-15).
        self._enqueue_timers: dict[int, threading.Timer] = {}
        self._timer_lock = threading.Lock()
        # Compiled forwarding rules (review P0-9): built lazily and keyed on
        # the rule list so a config swap recompiles. See _rule_engine().
        self._rule_engine_cache: tuple[object, RuleEngine] | None = None

    @property
    def spool_dir(self) -> Path:
        """Root directory where received DICOM studies are stored."""
        return self._spool_dir

    # --- read façade (web layer, review M6) --------------------------------
    #
    # Thin pass-throughs over the Database so callers (web/routes.py,
    # web/pipeline.py, web/console.py, recovery.py) never need to reach into
    # the private ``Spool._db``. The invariant stated in routes.py's
    # docstring — "all data access goes through the Spool/Database public
    # API" — is only real if Spool exposes these reads.

    def count_states(self) -> dict[str, int]:
        """Count studies grouped by lifecycle state."""
        return self._db.count_states()

    def count_studies(self, state: str | None = None, modality: str | None = None) -> int:
        """Count studies matching the given state/modality filters."""
        return self._db.count_studies(state=state, modality=modality)

    def list_studies_with_route_counts(
        self,
        state: str | None = None,
        modality: str | None = None,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List studies with per-study route counts, newest first."""
        return [
            dict(r)
            for r in self._db.list_studies_with_route_counts(
                state, modality, limit=limit, offset=offset
            )
        ]

    def get_study(self, study_id: int) -> dict[str, Any] | None:
        """Return one study row by id, or ``None``."""
        row = self._db.get_study(study_id)
        return dict(row) if row is not None else None

    def get_routes(self, study_id: int) -> list[dict[str, Any]]:
        """Return all routing tasks for a study."""
        return [dict(r) for r in self._db.get_routes(study_id)]

    def list_recent_routes(self, target_name: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """Latest studies routed to one destination (pipeline drill-down)."""
        return [dict(r) for r in self._db.list_recent_routes(target_name, limit=limit)]

    def count_routes_by_target(self) -> list[dict[str, Any]]:
        """Per-destination rollup across all studies (pipeline view)."""
        return [dict(r) for r in self._db.count_routes_by_target()]

    def list_audit_for_study(self, study_uid: str, *, limit: int = 50) -> list[dict[str, Any]]:
        """Audit events touching *study_uid*, newest first."""
        return [dict(r) for r in self._db.list_audit_for_study(study_uid, limit=limit)]

    def list_audit_events(
        self,
        event: str | None = None,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List audit events, newest first, optionally filtered by event name."""
        return [
            dict(r) for r in self._db.list_audit_events(event, limit=limit, offset=offset)
        ]

    def get_report(self, report_id: int) -> dict[str, Any] | None:
        """Return one report row by id, or ``None``."""
        row = self._db.get_report(report_id)
        return dict(row) if row is not None else None

    def list_reports(
        self,
        status: str | None = None,
        report_type: str | None = None,
        study_uid: str | None = None,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List reports with optional filters, newest first."""
        return [
            dict(r)
            for r in self._db.list_reports(
                status, report_type, study_uid, limit=limit, offset=offset
            )
        ]

    @property
    def database(self) -> Database:
        """The backing :class:`Database` (explicit, for AuditLog construction).

        Callers that genuinely need the Database object itself — currently only
        AuditLog wiring, which shares its transaction scope — go through this
        named accessor instead of the private ``_db`` attribute.
        """
        return self._db

    def stop(self) -> None:
        """Cancel all pending auto-enqueue timers (composition-root shutdown)."""
        with self._timer_lock:
            timers = list(self._enqueue_timers.values())
            self._enqueue_timers.clear()
        for timer in timers:
            timer.cancel()

    # --- storage layout ---------------------------------------------------

    def study_files(self, study_uid: str) -> list[Path]:
        """Return all DICOM files stored for *study_uid*.

        Single source of truth for the on-disk layout
        (``spool/{study_uid}/{series_uid}/{instance_uid}.dcm``); the forwarder
        handler and recovery scan must use this rather than re-deriving it.
        """
        study_dir = self._spool_dir / study_uid
        if not study_dir.exists():
            return []
        return sorted(study_dir.rglob("*.dcm"))

    # --- state transitions ------------------------------------------------

    def receive(
        self,
        study_uid: str,
        *,
        accession: str | None = None,
        mrn: str | None = None,
        patient_name: str | None = None,
        modality: str | None = None,
        study_description: str | None = None,
        study_date: str | None = None,
        num_series: int = 0,
        num_instances: int = 0,
    ) -> int:
        """Persist a received study and return its id.

        State: RECEIVING → RECEIVED. This is the "store before acknowledge"
        step that guarantees no data loss (PRD §3.4).
        """
        study_uid = validate_uid(study_uid, what="StudyInstanceUID")
        study_id = self._db.insert_study(
            study_uid=study_uid,
            accession=accession,
            mrn=mrn,
            patient_name=patient_name,
            modality=modality,
            study_description=study_description,
            study_date=study_date,
            num_series=num_series,
            num_instances=num_instances,
            state=StudyState.RECEIVED.value,
        )
        self._emit(
            STUDY_RECEIVED,
            {
                "study_id": study_id,
                "study_uid": study_uid,
                "accession": accession,
                "modality": modality,
            },
        )
        return study_id

    def store_instance(self, dataset: Any) -> int:
        """Persist one DICOM instance to disk and upsert its study row.

        Called by the receiver for each C-STORE before acknowledging.  Writes
        the instance to ``spool/{study_uid}/{series_uid}/{instance_uid}.dcm``
        and creates (or updates) the study row in state ``RECEIVED``.  Returns
        the study id.

        This is the store-before-acknowledge guarantee (PRD §3.4): by the time
        this returns, the file is on disk and the DB row exists.
        """
        study_uid = validate_uid(str(dataset.StudyInstanceUID), what="StudyInstanceUID")
        series_uid = validate_uid(str(dataset.SeriesInstanceUID), what="SeriesInstanceUID")
        instance_uid = validate_uid(str(dataset.SOPInstanceUID), what="SOPInstanceUID")

        out_dir = self._spool_dir / study_uid / series_uid
        # A directory created here only becomes durable once its *parent* has
        # been fsynced, so remember which ancestors are new (durability, H1).
        pre_existing = {p for p in (out_dir, out_dir.parent) if p.exists()}
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{instance_uid}.dcm"

        # Writes the file and the .tags sidecar only — no DB rows (review
        # P1-18). New-series detection now happens inside the transaction
        # below, where it is both ordered correctly against the instance_meta
        # insert (M11) and atomic with it. new_instance is resolved there too:
        # the file is written before this transaction so it survives a crash
        # the row does not, which makes a filesystem probe report "duplicate"
        # on the retry after a rolled-back receive (M11 follow-up).
        received_syntax, stored_syntax, num_bytes = self._apply_transfer_syntax(dataset, path)

        # Durability barrier. The instance bytes and the directory entries that
        # name them must reach stable storage BEFORE the row below says
        # RECEIVED — otherwise a crash between COMMIT and the writeback leaves
        # a study the database insists exists with no file behind it, and the
        # C-STORE has already been acknowledged (PRD §3.4, review H1).
        self._fsync_instance(path, dirs=self._dirs_to_sync(out_dir, pre_existing))

        # One transaction for the provenance row and the study upsert (review
        # P1-18): two BEGIN IMMEDIATE + synchronous=FULL commits per instance
        # became one. A failure here rolls both back and propagates, so the
        # receiver returns 0xC120 and the modality retries rather than a study
        # row landing without provenance.
        study_id, _new_series, new_instance = self._db.store_received_instance(
            study_uid=study_uid,
            series_uid=series_uid,
            instance_uid=instance_uid,
            file_path=str(path),
            received_syntax=received_syntax,
            stored_syntax=stored_syntax,
            num_bytes=num_bytes,
            accession=_tag(dataset, "AccessionNumber"),
            mrn=_tag(dataset, "PatientID"),
            patient_name=_tag(dataset, "PatientName"),
            modality=_tag(dataset, "Modality"),
        )
        if new_instance:
            # A genuinely NEW instance re-opens the study (the upsert demoted
            # it to RECEIVED): complete routes go back to waiting so the
            # updated study re-forwards to every destination.
            self._requeue_complete_routes(study_id)
        self._arm_auto_enqueue(study_id)
        return study_id

    @staticmethod
    def _dirs_to_sync(out_dir: Path, pre_existing: set[Path]) -> list[Path]:
        """Return the directories whose entries must be fsynced for durability.

        The file's own directory always needs a sync so the new name survives.
        Each ancestor that ``mkdir`` just created additionally needs *its*
        parent synced, otherwise a crash can lose the directory entry while
        the file inside it is durable — an orphaned, unreachable instance.
        """
        dirs = [out_dir]
        if out_dir not in pre_existing:
            dirs.append(out_dir.parent)
            if out_dir.parent not in pre_existing:
                dirs.append(out_dir.parent.parent)
        return dirs

    def _fsync_instance(self, path: Path, *, dirs: list[Path]) -> None:
        """Flush *path* and the directory entries naming it to stable storage.

        Raises ``OSError`` if the file itself cannot be flushed: acknowledging
        a C-STORE whose bytes we cannot guarantee are durable would silently
        lose the study, which is exactly what PRD §3.4 forbids (review H1).

        The ``.tags`` sidecar is deliberately *not* fsynced — it is derived
        data, rebuildable from the DICOM file, and syncing it would double the
        fsync cost of every receive for no durability benefit.

        Directory fsync is best-effort: Windows cannot open a directory handle
        at all, so there is no equivalent operation there.

        The file handle is opened read-write, not read-only: on Windows
        ``os.fsync`` → ``FlushFileBuffers`` fails on a handle without write
        access, while POSIX fsync only needs the descriptor (first real CI run,
        windows matrix — all store-before-ack paths died with EBADF).
        """
        with open(path, "r+b") as f:
            os.fsync(f.fileno())
        for directory in dirs:
            try:
                fd = os.open(directory, os.O_RDONLY)
            except OSError:
                logger.debug("directory fsync unavailable for %s (platform limitation)", directory)
                continue
            try:
                os.fsync(fd)
            except OSError:
                logger.warning("could not fsync directory %s", directory)
            finally:
                os.close(fd)

    def _requeue_complete_routes(self, study_id: int) -> None:
        """Reset complete routes to waiting when new content arrived.

        Only meaningful when the study was re-opened (new instance on a
        previously routed study); a fresh study has no routes yet.

        Every reset commits in ONE transaction. The intent was an
        ``executemany`` in the shape of ``Database.claim_next_tasks`` (one
        statement for N routes, one round trip), but ``spool/db.py`` is owned
        by another batch in this wave, so no new method can be added there.
        The resets instead JOIN a single outer transaction opened here and
        commit once — which delivers the property that actually matters: no
        partially-requeued study is observable to a concurrent
        ``claim_next_tasks`` between two route resets. Switching the loop body
        to a real executemany later is a mechanical db.py change with no
        caller-side impact.
        """
        routes = self._db.get_routes(study_id)
        row = self._db.get_study(study_id)
        if row is None or row["state"] != StudyState.RECEIVED.value:
            return  # SENDING (in-flight) or no routes — leave everything alone
        with self._db.transaction():
            for route in routes:
                if route["status"] == "complete":
                    self._db.reset_route_waiting(route["id"])
                    logger.info(
                        "new instance for study %d — route %d (%s) re-queued",
                        study_id,
                        route["id"],
                        route["target_name"],
                    )

    # --- auto-enqueue (US-03: auto-forward received studies) ---------------

    def _arm_auto_enqueue(self, study_id: int) -> None:
        """(Re)arm the auto-enqueue timer for the study just stored.

        One timer *per study*: each arriving instance replaces only that
        study's pending timer, so a multi-instance study is enqueued once
        the receiver has been idle on *it* for
        ``receiver.auto_enqueue_delay_sec`` — modalities never signal
        end-of-study, so per-study idle-time is the completion heuristic
        (S02-T5 note). Studies arriving interleaved each get their own
        countdown; one never cancels another (E1 dry-run regression).
        """
        if self._config is None:
            return
        delay = self._config.receiver.auto_enqueue_delay_sec
        if delay <= 0:
            # Disabled (0): enqueue synchronously after every store.
            self._auto_enqueue(study_id)
            return
        with self._timer_lock:
            stale = self._enqueue_timers.pop(study_id, None)
            if stale is not None:
                stale.cancel()
            timer = threading.Timer(delay, self._auto_enqueue, (study_id,))
            timer.daemon = True
            self._enqueue_timers[study_id] = timer
            timer.start()

    def _auto_enqueue(self, study_id: int) -> None:
        """Timer callback: enqueue the study unless it is past RECEIVED."""
        with self._timer_lock:
            self._enqueue_timers.pop(study_id, None)
        try:
            row = self._db.get_study(study_id)
            if row is None or row["state"] != StudyState.RECEIVED.value:
                return  # already queued/sending/terminal, or purged
            if self._config is None:
                return
            targets = [d for d in self._config.destinations if d.enabled]
            if not targets:
                return  # nothing to route to; study stays RECEIVED
            self.enqueue(study_id, targets)
            logger.info(
                "auto-enqueued study %s to %d destination(s)",
                row["study_uid"],
                len(targets),
            )
        except Exception:
            # A failed auto-enqueue must never kill the timer thread — the
            # study stays RECEIVED and can be re-forwarded from the console.
            logger.exception("auto-enqueue failed for study %d", study_id)

    # Syntaxes decompressed on receive when ``receiver.decompress_common``
    # is set (refinement §2.1: "common" = JPEG 2000 lossless, JPEG-LS, RLE,
    # JPEG lossless SV1). Rare/proprietary syntaxes are stored as-is.
    _COMMON_COMPRESSED_SYNTAXES = frozenset(
        {
            "1.2.840.10008.1.2.4.90",  # JPEG 2000 Image Compression (Lossless)
            "1.2.840.10008.1.2.4.57",  # JPEG Lossless, Non-Hierarchical (Process 14)
            "1.2.840.10008.1.2.4.70",  # JPEG Lossless, Non-Hierarchical SV1
            "1.2.840.10008.1.2.4.80",  # JPEG-LS Lossless
            "1.2.840.10008.1.2.5",  # RLE Lossless
        }
    )

    def _apply_transfer_syntax(
        self, dataset: Any, path: Path
    ) -> tuple[str, str, int]:
        """Persist *dataset* to *path*, honoring the decompression policy.

        Keeps the ORIGINAL transfer syntax in a private provenance element
        (creator ``mercure-gateway``) when the stored bytes differ from what
        was received — decompression must not lose provenance (S02-T3).

        Returns ``(received_syntax, stored_syntax, num_bytes)`` — the
        provenance facts the caller records in one transaction with the study
        row (review P1-18). This method is now pure file I/O: it writes no
        database rows, so a DB failure cannot leave the file on disk with a
        half-recorded study.
        """
        from pydicom.dataset import FileMetaDataset

        received_syntax = "1.2.840.10008.1.2.1"
        if getattr(dataset, "file_meta", None) and dataset.file_meta.get("TransferSyntaxUID", None):
            received_syntax = str(dataset.file_meta.TransferSyntaxUID)

        stored_syntax = received_syntax
        decompress_requested = self._config is not None and self._config.receiver.decompress_common
        if decompress_requested and received_syntax in self._COMMON_COMPRESSED_SYNTAXES:
            try:
                dataset.decompress()
                stored_syntax = "1.2.840.10008.1.2.1"  # Explicit VR Little Endian
            except Exception:
                # Codec unavailable (e.g. no JPEG 2000 library) — store as-is
                # rather than failing the receive; provenance stays correct.
                logger.warning(
                    "decompress_common=True but codec for %s unavailable — storing as-is",
                    received_syntax,
                )
                stored_syntax = received_syntax

        if stored_syntax != received_syntax:
            block = dataset.private_block(0x000B, "mercure-gateway", create=True)
            block.add_new(0x01, "UI", received_syntax)  # original syntax provenance

        if not getattr(dataset, "file_meta", None):
            dataset.file_meta = FileMetaDataset()
        dataset.file_meta.MediaStorageSOPClassUID = str(dataset.SOPClassUID)
        dataset.file_meta.MediaStorageSOPInstanceUID = str(dataset.SOPInstanceUID)
        dataset.file_meta.TransferSyntaxUID = stored_syntax
        dataset.save_as(str(path), enforce_file_format=True)

        # ``.tags`` sidecar: flat JSON summary for routing/display (S02-T4).
        from mercure_gateway.spool.tags import write_tags_file

        write_tags_file(dataset, path)

        return received_syntax, stored_syntax, path.stat().st_size

    def enqueue(self, study_id: int, targets: list[Destination]) -> None:
        """Schedule a study for delivery to ``targets``.

        State: RECEIVED → QUEUED. One routing task is created per target.
        When ``config.forwarding_rules`` contains a matching rule, only the
        rule's named targets are routed (MVP modality filter, refinement §3.1);
        otherwise every enabled target receives the study.

        If a forwarding rule narrowed the target set to nothing — a rule names
        a destination that was renamed, removed, or copied from another
        profile — the study stays RECEIVED rather than going QUEUED with zero
        routes. QUEUED-with-no-routes is unreachable from the panel (the
        Enqueue action only renders for RECEIVED, and /retry only resets
        routes that exist) and would need direct DB access to recover.

        This guard is deliberately scoped to the *rule-filtered* case. A
        caller that passes only disabled targets (an operator's explicit
        choice) keeps the legacy QUEUED-with-no-routes behavior — see
        ``test_disabled_target_is_skipped`` — which is a deliberate no-op,
        not a misconfiguration.
        """
        filtered, rule_narrowed = self._route_targets(study_id, targets)
        routed = [t for t in filtered if t.enabled]
        if not routed and rule_narrowed:
            # A matching rule narrowed the set to destinations that are stale,
            # renamed, or disabled — a misconfiguration. Staying RECEIVED
            # keeps the panel rescue path open; QUEUED-with-zero-routes would
            # be unreachable (Enqueue renders only for RECEIVED, /retry only
            # resets routes that exist) and would need direct DB access.
            logger.warning(
                "study %s: forwarding rules matched no enabled destination "
                "(stale rule target?); staying RECEIVED",
                self.study_uid(study_id),
            )
            return
        for target in routed:
            self._db.insert_route(
                study_id=study_id,
                target_name=target.name,
                target_type=target.type,
            )
        self._db.set_study_state(study_id, StudyState.QUEUED.value)
        self._emit(
            STUDY_QUEUED,
            {
                "study_id": study_id,
                "study_uid": self.study_uid(study_id),
                "targets": [t.name for t in routed],
            },
        )

    def _route_targets(
        self, study_id: int, targets: list[Destination]
    ) -> tuple[list[Destination], bool]:
        """Filter ``targets`` by the configured forwarding rules.

        Delegates to the shared :class:`~mercure_gateway.rules.RuleEngine` so
        the panel's rule preview and enqueue-time routing are one
        implementation (review P0-9).  Before that, the spool hand-rolled a
        ``modality:`` matcher that understood neither priority nor the
        ``Tag=value`` grammar, while the preview engine raised
        ``RuleSyntaxError`` on every deployed ``modality:CT`` rule — so
        preview and production routed differently and neither knew it.

        Also returns whether a rule actually narrowed the set — the caller
        distinguishes "rule matched but its targets are stale/disabled" (a
        misconfiguration; the study should not advance) from "the caller passed
        only disabled destinations" (a deliberate no-op).

        A malformed rule fails *open*: the engine skips it (keeping the good
        rules in the same config), and a study that rule would have matched
        takes the default route.  Stranding studies in RECEIVED because an
        operator typo'd a rule is worse than over-delivering, and the mistake
        is surfaced by the config lint at save time plus the engine's own log
        rather than hidden here.  The ``except`` below is defence in depth —
        the engine does not raise after the per-rule skip — so a future change
        that makes construction strict still degrades instead of crashing the
        enqueue path.
        """
        rules = self._config.forwarding_rules if self._config is not None else []
        if not rules:
            return targets, False
        study = self._db.get_study(study_id)
        modality = str(study["modality"]).upper() if study and study["modality"] else None
        # Only the Modality tag is available at enqueue time; a rule on any
        # other tag simply cannot match here (it still previews correctly when
        # given a full tag set).
        tags = {"Modality": modality} if modality else {}
        try:
            matched_names, matched_any = self._rule_engine().match(tags)
        except RuleSyntaxError as exc:
            logger.error(
                "study %s: a forwarding rule is malformed (%s); routing to all "
                "destinations — fix the rule in the destinations panel",
                self.study_uid(study_id),
                exc,
            )
            return targets, False
        if not matched_any:
            return targets, False
        wanted = set(matched_names)
        return [t for t in targets if t.name in wanted], True

    def _rule_engine(self) -> RuleEngine:
        """Compile the configured rules once, recompiling if they were swapped.

        A config save needs a process restart to reach the spool (see
        ``_config_pending_restart``), so the compile is cached on the rule
        list's identity rather than rebuilt per study.  Constructing the engine
        compiles every rule, so a malformed rule raises here — the caller fails
        open and this stays uncached, keeping the error live per study.
        """
        rules = self._config.forwarding_rules if self._config is not None else []
        cached = self._rule_engine_cache
        if cached is not None and cached[0] is rules:
            return cached[1]
        engine = RuleEngine(rules)
        self._rule_engine_cache = (rules, engine)
        return engine

    def claim_next(self, limit: int = 1) -> list[ClaimedTask]:
        """Atomically claim the next waiting task(s).

        State: QUEUED → SENDING. Safe under concurrency via ``BEGIN IMMEDIATE``.
        """
        rows = self._db.claim_next_tasks(limit)
        return [
            ClaimedTask(
                route_id=row["route_id"],
                study_id=row["study_id"],
                target_name=row["target_name"],
                target_type=row["target_type"],
            )
            for row in rows
        ]

    def claim_route(self, route_id: int) -> ClaimedTask | None:
        """Atomically claim one specific route (retry path), if claimable.

        Succeeds only when the route is ``waiting`` and its study is not
        ``FAILED``. Returns ``None`` otherwise.
        """
        row = self._db.claim_route(route_id)
        if row is None:
            return None
        return ClaimedTask(
            route_id=row["route_id"],
            study_id=row["study_id"],
            target_name=row["target_name"],
            target_type=row["target_type"],
        )

    def complete(self, study_id: int, target_name: str) -> StudyState:
        """Mark a target delivered.

        When every target for the study is complete the study transitions
        SENDING → SENT; otherwise it stays SENDING. Returns the new study state.

        Every write here runs in ONE transaction (review P2-x): the route
        completion, the study state, and the retention stamp commit together.
        They used to be up to four separate commits, and a concurrent
        claim_next_tasks / complete could read ``route = complete`` with the
        study still SENDING in the gap between them.
        """
        routes = self._db.get_routes(study_id)
        route = next((r for r in routes if r["target_name"] == target_name), None)
        if route is None:
            raise KeyError(f"no routing task for target {target_name!r} on study {study_id}")
        # Resolved inside the transaction, emitted outside it. AuditLog.append
        # JOINS an open transaction rather than opening its own (db.transaction
        # nests by joining), so emitting in here would fire the hub sink while
        # the state change is still uncommitted — reporting an event the outer
        # commit could still roll back, against the audit invariant that only
        # persisted events are reported (audit/__init__.py). Paying one extra
        # transaction after the commit keeps commit-then-audit ordering.
        all_sent = False
        with self._db.transaction():
            self._db.mark_route_sent(route["id"])
            if self._db.all_routes_complete(study_id):
                self._db.set_study_state(study_id, StudyState.SENT.value)
                self._db.set_retention_delivered(study_id)
                all_sent = True
            else:
                self._db.set_study_state(study_id, StudyState.SENDING.value)
        if all_sent:
            self._emit(
                STUDY_SENT,
                {
                    "study_id": study_id,
                    "study_uid": self.study_uid(study_id),
                    "target_name": target_name,
                },
            )
        return self.state(study_id)

    def fail(
        self, study_id: int, target_name: str, error: str, *, max_attempts: int = 5
    ) -> StudyState:
        """Record a failed delivery for a target.

        State: SENDING → ERROR while retries remain, → FAILED once
        ``max_attempts`` is exhausted (local copy is retained; PRD §3.3). Returns
        the new study state.

        The route error and the study state commit in one transaction, so a
        route is never observed as ``error`` on a study that still reads
        SENDING. The audit event is emitted after the transaction closes —
        see :meth:`complete` for why.
        """
        routes = self._db.get_routes(study_id)
        route = next((r for r in routes if r["target_name"] == target_name), None)
        if route is None:
            raise KeyError(f"no routing task for target {target_name!r} on study {study_id}")
        # PRE-INCREMENT COMPARISON — load-bearing. claim_next bumps ``attempts``
        # when it takes the route; this snapshot therefore already counts the
        # attempt in flight, and ``attempts >= max_attempts`` fires on the call
        # that exhausts the budget — fail(max_attempts=1) goes FAILED on the
        # FIRST call (test_forwarder.py, test_audit_coverage.py). The read
        # predates the write transaction below and keeps that arithmetic exactly
        # as it was; moving it inside the transaction would read the same value
        # (claim already committed), but the ordering is asserted elsewhere, so
        # it stays here.
        attempts = int(route["attempts"])
        terminal = False
        with self._db.transaction():
            self._db.mark_route_error(route["id"], error)
            if attempts >= max_attempts:
                self._db.set_study_state(study_id, StudyState.FAILED.value)
                terminal = True
            else:
                self._db.set_study_state(study_id, StudyState.ERROR.value)
        if terminal:
            self._emit(
                STUDY_FAILED,
                {
                    "study_id": study_id,
                    "study_uid": self.study_uid(study_id),
                    "target_name": target_name,
                    "error": error,
                    "attempts": attempts,
                },
            )
        return self.state(study_id)

    def enqueue_study(self, study_id: int) -> int:
        """Operator action: route a RECEIVED study to the enabled destinations.

        This is the recovery path ``_auto_enqueue`` refers to when it leaves
        a study in RECEIVED — e.g. instances arrived while no destination was
        enabled (destinations added after receipt), or auto-enqueue failed
        and logged the exception. ``reforward_study`` cannot reach these
        studies: it iterates *existing* routes, and a stranded study has
        none (E1 dry run, 2026-09-16).

        Returns the number of routes created (0 if the study was already
        routed). Raises ``KeyError`` for an unknown id, ``ValueError`` if the
        study is in a terminal state (SENT must never be re-routed —
        re-delivery is not idempotent) or if no destination is enabled
        (routing would be a silent no-op, which hides a config error).
        """
        row = self._db.get_study(study_id)
        if row is None:
            raise KeyError(f"no study with id {study_id}")
        if row["state"] == StudyState.SENT.value:
            raise ValueError(f"study {study_id} is SENT — terminal, will not re-route")
        if self._config is None:
            raise ValueError("no configuration loaded; cannot determine targets")
        targets = [d for d in self._config.destinations if d.enabled]
        if not targets:
            raise ValueError(
                "no enabled destination to enqueue study "
                f"{row['study_uid']} to — enable a destination first"
            )
        if self._db.get_routes(study_id):
            return 0  # already routed; idempotent, not an error
        before = len(self._db.get_routes(study_id))
        self.enqueue(study_id, targets)
        created = len(self._db.get_routes(study_id)) - before
        if not created:
            # A forwarding rule narrowed the enabled set to nothing — a stale
            # rule target. Report the config error rather than letting the
            # caller (and the panel) read it as success.
            raise ValueError(
                f"forwarding rules matched no enabled destination for study "
                f"{row['study_uid']} — a rule target is stale, renamed, or disabled"
            )
        return created

    def reforward(self, study_id: int, target_name: str) -> None:
        """Return an errored route to the waiting queue for a manual re-forward."""
        routes = self._db.get_routes(study_id)
        route = next((r for r in routes if r["target_name"] == target_name), None)
        if route is None:
            raise KeyError(f"no routing task for target {target_name!r} on study {study_id}")
        self._db.reset_route_waiting(route["id"])
        self._db.set_study_state(study_id, StudyState.QUEUED.value)

    def reforward_study(self, study_id: int) -> int:
        """Re-queue every non-complete route of *study_id* for delivery.

        Resets the per-route retry budget. Used by the web admin "retry"
        action; returns the number of routes re-queued.
        """
        routes = self._db.get_routes(study_id)
        requeued = 0
        for route in routes:
            if route["status"] != "complete":
                self._db.reset_route_waiting(route["id"])
                self._db.reset_route_attempts(route["id"])
                requeued += 1
        if requeued:
            self._db.set_study_state(study_id, StudyState.QUEUED.value)
            self._emit_retry_manual(study_id, requeued)
        return requeued

    def _emit(self, event: str, detail: dict[str, Any]) -> None:
        """Append an audit event when an AuditLog is wired; otherwise no-op."""
        if self._audit is not None:
            self._audit.append(event, detail)

    def _emit_retry_manual(self, study_id: int, requeued: int) -> None:
        """Append a RETRY_MANUAL audit event when an AuditLog is wired."""
        if self._audit is None:
            return
        study = self._db.get_study(study_id)
        if study is None:
            return
        self._emit(
            RETRY_MANUAL,
            {
                "study_id": study_id,
                "study_uid": str(study["study_uid"]),
                "requeued_routes": requeued,
            },
        )

    def state(self, study_id: int) -> StudyState:
        """Return the current study state."""
        row = self._db.get_study(study_id)
        if row is None:
            raise KeyError(f"unknown study {study_id}")
        return StudyState(row["state"])

    def route_attempts(self, study_id: int, target_name: str) -> int:
        """Return the number of delivery attempts made for a study/target pair."""
        routes = self._db.get_routes(study_id)
        for route in routes:
            if route["target_name"] == target_name:
                return int(route["attempts"])
        raise KeyError(f"no routing task for target {target_name!r} on study {study_id}")

    def study_uid(self, study_id: int) -> str:
        """Return the Study Instance UID for a study id, or raise KeyError."""
        row = self._db.get_study(study_id)
        if row is None:
            raise KeyError(f"unknown study {study_id}")
        return str(row["study_uid"])

    # --- retention (US-04: never auto-delete undelivered copies) ---------

    def mark_delivered(self, study_id: int) -> None:
        """Stamp the delivery-completed time used by the retention purger."""
        self._db.set_retention_delivered(study_id)

    def purge_delivered(self) -> int:
        """Delete files + rows for SENT studies past the retention window.

        Only studies in state ``SENT`` with an expired
        ``retention_delivered_at`` are removed. Undelivered studies
        (FAILED/ERROR/QUEUED/RECEIVED) are never touched (US-04/PRD §3.4-5).
        Returns the number of studies purged.

        Effective window (S10-T5): with ``usb_mode.enabled`` the aggressive
        ``usb_mode.retention_delivered_hours`` window applies; otherwise
        ``storage.retention_delivered_days`` is used (day-granular default).
        """
        if self._config is None:
            return 0
        retention_hours = self._effective_retention_hours()
        rows = self._db.list_purgable_delivered(retention_hours)
        purged = 0
        for row in rows:
            if self._purge_study_dir(raw_uid=str(row["study_uid"]), study_id=int(row["id"])):
                purged += 1
        if purged:
            logger.info("retention: purged %d delivered study(ies)", purged)
        return purged

    def _effective_retention_hours(self) -> int:
        """Retention window in hours for the current config profile (S10-T5)."""
        config = self._config
        if config is None:
            return 0
        if config.usb_mode.enabled:
            return config.usb_mode.retention_delivered_hours
        return config.storage.retention_delivered_days * 24

    def purge_oldest_delivered(self) -> bool:
        """Purge the single oldest delivered study (disk-full auto-recovery).

        Only fully-delivered (``SENT``) studies are eligible; undelivered /
        FAILED studies are never auto-removed (US-04).  Returns ``True`` when a
        study was actually removed, ``False`` otherwise — including when an
        eligible study *exists* but its files cannot be deleted. The callers
        treat ``True`` as "made progress, keep going": returning it here for a
        failed delete made the disk-full loop re-select the same un-deletable
        study every iteration until the budget tripped, reporting a capacity
        race that was really one locked file.
        """
        row = self._db.list_oldest_delivered()
        if row is None:
            return False
        if not self._purge_study_dir(raw_uid=str(row["study_uid"]), study_id=int(row["id"])):
            return False
        logger.info("disk-full: purged delivered study %s", row["study_uid"])
        return True

    def spool_num_bytes(self) -> int:
        """Total bytes of persisted DICOM instances (storage-cap checks, M3)."""
        return self._db.spool_num_bytes()

    def has_purgeable_delivered(self) -> bool:
        """Whether a delivered study is currently eligible for auto-purge.

        Distinguishes "nothing eligible" from "eligible but un-deletable" for
        the disk monitor's diagnostics — the two look identical to a caller
        that only sees ``purge_oldest_delivered`` return ``False``.
        """
        return self._db.list_oldest_delivered() is not None

    def _purge_study_dir(self, raw_uid: str, study_id: int) -> bool:
        """Delete a study's spool files and database row; idempotent.

        Returns whether the DB row was deleted. The row is deleted *after* the
        files: a failed delete (locked file, read-only media) leaves the row in
        place, so the study stays visible in the queue and a later purge pass
        can retry — the previous ``ignore_errors=True``-then-delete-row order
        silently removed queued studies whose files never left the disk. Callers
        that report progress must key off this return value, not on having been
        handed an eligible row.
        """
        study_dir = self._spool_dir / raw_uid
        if study_dir.exists():
            try:
                shutil.rmtree(study_dir)
            except OSError as exc:
                logger.error(
                    "could not delete spool files for study %s — DB row kept: %s",
                    raw_uid,
                    exc,
                )
                return False
        self._db.delete_study(study_id)
        return True

    def queued_count(self) -> int:
        """Number of studies currently waiting or sending (not terminal)."""
        counts = self._db.count_states()
        return sum(
            n
            for s, n in counts.items()
            if s not in (StudyState.SENT.value, StudyState.FAILED.value)
        )
