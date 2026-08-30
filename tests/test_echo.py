"""S06-T6 (RED): Connectivity echo service (Flow A-7).

The echo service tests C-ECHO to a DICOM destination and returns a status
string consumed by the web wizard — ``ok``, ``timeout``, ``refused``, or
``error``.  The service is tested against a fake C-ECHO SCP.
"""

from __future__ import annotations

import pytest

from mercure_gateway.config import DICOMDestination


def test_echo_ok_to_real_scp() -> None:
    """C-ECHO to a listening SCP returns ``ok``."""

    from pynetdicom import AE
    from pynetdicom.sop_class import Verification

    from mercure_gateway.web.echo import echo_destination

    scp_ae = AE()
    scp_ae.add_supported_context(Verification)
    srv = scp_ae.start_server(("127.0.0.1", 0), block=False)
    port = srv.socket.getsockname()[1]

    dest = DICOMDestination(name="test", host="127.0.0.1", port=port, aet_target="ECHO_TEST")
    status = echo_destination(dest)
    assert status == "ok", f"expected ok, got {status!r}"
    scp_ae.shutdown()


def test_echo_refused_no_scp() -> None:
    """C-ECHO to a port with nothing listening returns ``refused``."""
    from mercure_gateway.web.echo import echo_destination

    dest = DICOMDestination(name="refused", host="127.0.0.1", port=19999, aet_target="NOPE")
    status = echo_destination(dest)
    assert status == "refused", f"expected refused, got {status!r}"


def test_echo_timeout() -> None:
    """C-ECHO to a non-responsive host returns ``timeout``."""
    from mercure_gateway.web.echo import echo_destination

    dest = DICOMDestination(name="timeout", host="10.255.255.1", port=104, aet_target="DARK")
    import os
    if os.name == "nt":
        pytest.skip("timeout test unreliable on Windows")
    status = echo_destination(dest, timeout_sec=1)
    assert status == "timeout", f"expected timeout, got {status!r}"


def test_echo_api_endpoint_ok() -> None:
    """POST /api/echo returns ok for a reachable SCP (Flow A-7)."""
    from conftest import FakeForwarder, FakeReceiver
    from fastapi.testclient import TestClient
    from pynetdicom import AE
    from pynetdicom.sop_class import Verification

    from mercure_gateway.config import default_config
    from mercure_gateway.spool import Spool
    from mercure_gateway.spool.db import mem_database
    from mercure_gateway.web import create_app

    scp_ae = AE()
    scp_ae.add_supported_context(Verification)
    srv = scp_ae.start_server(("127.0.0.1", 0), block=False)
    port = srv.socket.getsockname()[1]

    spool = Spool(mem_database())
    app = create_app(default_config(), spool)
    app.state.receiver = FakeReceiver()
    app.state.forwarder = FakeForwarder()
    client = TestClient(app)
    r = client.post(
        "/api/echo",
        json={"name": "probe", "host": "127.0.0.1", "port": port, "aet": "ANY"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    scp_ae.shutdown()