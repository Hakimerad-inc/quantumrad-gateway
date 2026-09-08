"""Recovery scan for the USB dongle gateway variant.

On startup (especially after an unclean shutdown or hot-unplug), the recovery
scanner reconciles the spool directory (DICOM files on disk) with the
database (study rows and routing tasks).  It follows the protocol from
``usb-dongle-gateway-spec §7.3``:

1. Check for a shutdown marker from the previous session.
2. Scan ``spool_dir`` for ``*.dcm`` files.
3. Compare discovered study UIDs against the database.
4. For studies in non-terminal states (RECEIVING, SENDING, ERROR):
   mark as RECEIVED and reset their routes for re-forward.
5. For files on disk without DB rows: create RECEIVED study rows.
6. For DB rows without matching files: mark as incomplete.
7. Clear the shutdown marker.

The scan only runs when the previous shutdown was *unclean* (no marker —
crash, power-loss, yanked device) or the caller forces it. A graceful
shutdown writes the marker (``hotplug.write_shutdown_marker``) as its last
filesystem act, so its presence proves the database is authoritative and
the O(total-files) filesystem walk is skipped so the DICOM port binds fast.

This module is designed to be called once at startup, before the receiver
and forwarder are started.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from mercure_gateway.hotplug import clear_shutdown_marker, has_shutdown_marker
from mercure_gateway.spool import Spool, StudyState, validate_uid

__all__ = ["RecoveryResult", "recover"]

logger = logging.getLogger(__name__)


@dataclass
class RecoveryResult:
    """Outcome of a recovery scan."""

    ran: bool = False
    had_marker: bool = False
    studies_recovered: int = 0
    studies_orphaned: int = 0
    files_without_db: int = 0
    cleaned: bool = False
    errors: list[str] = field(default_factory=list)


def _discover_study_uids(spool_dir: Path) -> dict[str, list[Path]]:
    """Scan *spool_dir* for DICOM files and return ``{study_uid: [paths]}``.

    Expects the layout ``spool/{study_uid}/{series_uid}/{instance_uid}.dcm``
    written by ``Spool.store_instance``. Directory names that fail UID
    validation are ignored (they cannot be safely registered as studies).
    """
    studies: dict[str, list[Path]] = {}
    if not spool_dir.exists():
        return studies
    for dcm_file in spool_dir.rglob("*.dcm"):
        # spool / study_uid / series_uid / instance_uid.dcm
        parts = dcm_file.relative_to(spool_dir).parts
        if len(parts) >= 2:
            try:
                study_uid = validate_uid(parts[0], what="StudyInstanceUID")
            except ValueError:
                logger.warning("ignoring non-UID spool directory %r during recovery", parts[0])
                continue
            studies.setdefault(study_uid, []).append(dcm_file)
    return studies


def _count_instances(spool_dir: Path, study_uid: str) -> tuple[int, int]:
    """Return ``(num_series, num_instances)`` for a study on disk."""
    study_dir = spool_dir / study_uid
    if not study_dir.exists():
        return 0, 0
    series_dirs = [d for d in study_dir.iterdir() if d.is_dir()]
    num_instances = sum(
        len(list(sd.glob("*.dcm")))
        for sd in series_dirs
    )
    return len(series_dirs), num_instances


def recover(spool: Spool, *, force: bool = False) -> RecoveryResult:
    """Run a recovery scan on the spool directory.

    This should be called once at startup.  It checks for a shutdown marker,
    scans the filesystem, and reconciles with the database.  Pass ``force``
    to scan even when the previous shutdown was clean.

    Parameters
    ----------
    spool:
        The gateway's :class:`Spool` instance (owns both the directory and DB).
    force:
        Run the filesystem scan even without an unclean-shutdown marker.

    Returns
    -------
    RecoveryResult
        Summary of what was found and fixed. ``ran`` is ``False`` when the
        scan was skipped (clean shutdown, not forced).
    """
    result = RecoveryResult()
    spool_dir = spool.spool_dir

    # Step 1: Check for shutdown marker. A marker means the previous session
    # shut down gracefully (it writes the marker as its last filesystem act):
    # the DB is authoritative and the scan is skipped. A MISSING marker means
    # crash / power-loss / yanked device — exactly when recovery must run.
    result.had_marker = has_shutdown_marker(spool_dir)
    if result.had_marker:
        # Clear the stale marker even when skipping: otherwise the *next*
        # crash would also be mistaken for a clean shutdown.
        clear_shutdown_marker(spool_dir)
        result.cleaned = True
        if not force:
            logger.debug("clean shutdown — recovery scan skipped")
            return result
    result.ran = True
    if force and not result.had_marker:
        logger.info("Recovery scan forced")

    # Step 2: Discover files on disk
    disk_studies = _discover_study_uids(spool_dir)
    logger.info("Found %d study directories on disk", len(disk_studies))

    # Step 3: Get all studies from DB
    db_studies = spool._db.list_studies()
    db_uids = {row["study_uid"]: row for row in db_studies}

    # Step 4: Reconcile — files on disk not in DB
    for study_uid in disk_studies:
        if study_uid not in db_uids:
            # Create a RECEIVED study row for orphaned files
            num_series, num_instances = _count_instances(spool_dir, study_uid)
            try:
                spool.receive(
                    study_uid,
                    num_series=num_series,
                    num_instances=num_instances,
                )
                result.files_without_db += 1
                logger.info(
                    "Created study row for orphaned files: %s (%d series, %d instances)",
                    study_uid, num_series, num_instances,
                )
            except Exception as exc:
                msg = f"Failed to create study for {study_uid}: {exc}"
                logger.error(msg)
                result.errors.append(msg)

    # Step 5: Reconcile — studies in non-terminal states, and reset their
    # routes (sending AND errored) so the forwarder can re-claim them.
    non_terminal = {
        StudyState.RECEIVING.value,
        StudyState.SENDING.value,
        StudyState.ERROR.value,
    }
    for study_uid, row in db_uids.items():
        state = row["state"]
        study_id = row["id"]
        has_files = study_uid in disk_studies

        if state in non_terminal:
            if has_files:
                # Study was interrupted mid-transfer; mark as RECEIVED
                # so the forwarder can re-queue it
                spool._db.set_study_state(study_id, StudyState.RECEIVED.value)
                result.studies_recovered += 1
                logger.info(
                    "Recovered study %s (was %s → RECEIVED)", study_uid, state,
                )
                for route in spool._db.get_routes(study_id):
                    if route["status"] in ("sending", "error"):
                        spool._db.reset_route_waiting(route["id"])
                        logger.info(
                            "Reset route %d (target=%s) from %s to waiting",
                            route["id"], route["target_name"], route["status"],
                        )
            else:
                # DB row exists but no files on disk — mark incomplete
                spool._db.set_study_state(study_id, StudyState.ERROR.value)
                result.studies_orphaned += 1
                logger.warning(
                    "Study %s has DB row but no files on disk — marked ERROR", study_uid,
                )

    # Step 7 (marker already cleared at step 1 — the scan never leaves a
    # stale marker behind regardless of how it exits).
    if result.studies_recovered or result.files_without_db:
        logger.info(
            "Recovery complete: %d studies recovered, %d orphaned files registered, "
            "%d orphaned DB rows found",
            result.studies_recovered, result.files_without_db, result.studies_orphaned,
        )

    return result
