"""C-ECHO SCU connectivity probe for the web wizard (Flow A-7, S06-T6).

``echo_destination`` verifies a DICOM destination is reachable by opening a
C-ECHO association.  It returns one of the status strings the web wizard maps
to green/red per step:

- ``ok``      — association established and C-ECHO succeeded
- ``refused`` — TCP connection succeeded but the association was rejected
- ``timeout`` — no TCP response (dark/unroutable host) within ``timeout_sec``
- ``error``   — DNS resolution failure or other unrecoverable error
"""

from __future__ import annotations

import socket
import time

from pynetdicom import AE
from pynetdicom.sop_class import Verification  # type: ignore[attr-defined]

from mercure_gateway.config import DICOMDestination

__all__ = ["echo_destination"]


def echo_destination(
    destination: DICOMDestination,
    *,
    timeout_sec: float = 5.0,
) -> str:
    """Probe *destination* with a DICOM C-ECHO request.

    Returns ``ok`` / ``refused`` / ``timeout`` / ``error`` per the mapping in
    the module docstring.
    """
    if destination.type != "dicom":
        return "error"

    probe = _probe_tcp(destination.host, destination.port, timeout_sec)
    if probe != "ok":
        return probe

    ae = AE(ae_title=destination.aet_source)
    # Verification (C-ECHO) first; some SCPs (e.g. QuantumPACS) only
    # advertise Storage contexts and reject the Verification SOP class, so
    # fall back to negotiating a Storage context to at least prove the
    # association layer works end to end.
    ae.add_requested_context(Verification)
    try:
        assoc = ae.associate(
            destination.host,
            destination.port,
            ae_title=destination.aet_target,
        )
    except Exception:  # noqa: BLE001 — boundary: any association failure
        return "refused"
    if not assoc.is_established:
        from pynetdicom.presentation import build_context
        from pynetdicom.sop_class import CTImageStorage  # noqa: F401 — any storage UID works

        ae.requested_contexts = [build_context(CTImageStorage)]
        try:
            assoc = ae.associate(
                destination.host,
                destination.port,
                ae_title=destination.aet_target,
            )
        except Exception:  # noqa: BLE001
            return "refused"
        if not assoc.is_established:
            return "refused"
        # Association accepted on a Storage context — reachable, though we
        # cannot C-ECHO it. Treat TCP+association success as "ok".
        assoc.release()
        return "ok"
    try:
        status = assoc.send_c_echo()
        if status is None:
            return "timeout"
        if status.Status == 0x0000:
            return "ok"
        return "refused"
    finally:
        assoc.release()


def _probe_tcp(host: str, port: int, timeout_sec: float) -> str:
    """Classify a TCP connect attempt to ``host:port``.

    Returns ``ok`` on success, or the status string describing the failure:
    ``refused`` (connection refused / association-level reject), ``timeout``
    (no response), or ``error`` (DNS failure).
    """
    try:
        socket.gethostbyname(host)
    except OSError:
        return "error"
    deadline = time.monotonic() + timeout_sec
    try:
        with socket.create_connection((host, port), timeout=max(0.1, timeout_sec)):
            return "ok"
    except TimeoutError:
        return "timeout"
    except ConnectionRefusedError:
        return "refused"
    except OSError:
        # Any other OS error: was it still within the deadline (→ timeout)?
        if time.monotonic() < deadline:
            return "timeout"
        return "error"
