"""S01-T5 interop: real gateway hub clients against the bookkeeper stub.

Everything up to this file exercised the bookkeeper with in-process fakes
(``FakeHub``, ``requests.post`` mocks). This test runs the *actual* HTTP stack:
a live (loopback) uvicorn instance of ``test-rig/bookkeeper/bookkeeper.py``
implementing the documented contract (``docs/dev/hub-anchor-api.md`` + the
TD-19 ``Token`` auth scheme), with the unmodified gateway clients pointed at
it:

* ``HubClient.register()`` → ``POST /register-gateway``
* ``HubEventStreamer`` → ``POST /events`` (batch delivery observed)
* ``SignedHeadAnchorer`` → ``POST /anchor`` → signed JSONL → offline
  verification via ``scripts/verify_audit_anchors.py`` (the operator path)
* tamper check: a mutated head in the JSONL must fail offline verification
* auth check: a wrong api_key must NOT be accepted (401 → gateway degrades)

Skips are never acceptable here — the stub runs in-process, no Docker needed.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import socket
import sys
import threading
import time
from pathlib import Path
from types import ModuleType

import pytest
import uvicorn
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mercure_gateway.audit.anchoring import SignedHeadAnchorer, verify_anchor_signatures
from mercure_gateway.hub_client import HubClient
from mercure_gateway.hub_events import HubEventStreamer

API_KEY = "interop-test-key"


def _load_stub_module() -> ModuleType:
    """Import the bookkeeper stub by path (test-rig/ is not a package)."""
    path = Path(__file__).resolve().parents[1] / "test-rig" / "bookkeeper" / "bookkeeper.py"
    spec = importlib.util.spec_from_file_location("bookkeeper_stub", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Register before exec: pydantic resolves the stub models' deferred
    # annotations via sys.modules["bookkeeper_stub"] at validation time.
    sys.modules["bookkeeper_stub"] = module
    spec.loader.exec_module(module)
    return module


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _StubServer:
    """The bookkeeper stub on a live loopback socket (uvicorn in a thread)."""

    def __init__(self) -> None:
        stub = _load_stub_module()
        self.signing_key = Ed25519PrivateKey.generate()
        pub = self.signing_key.public_key()
        self.public_key_b64 = base64.b64encode(pub.public_bytes_raw()).decode("ascii")
        self.state = stub.BookkeeperState(API_KEY, self.signing_key)
        self.port = _free_port()
        app = stub.create_app(self.state)
        config = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 15.0
        import requests

        while time.monotonic() < deadline:
            try:
                if requests.get(f"http://127.0.0.1:{self.port}/health", timeout=0.5).ok:
                    return
            except Exception:
                time.sleep(0.05)
        raise RuntimeError("bookkeeper stub did not start")

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def shutdown(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10.0)


@pytest.fixture()
def hub() -> _StubServer:
    server = _StubServer()
    try:
        yield server
    finally:
        server.shutdown()


# ── POST /register-gateway ──────────────────────────────────────────────


def test_hub_client_registers_against_stub(hub: _StubServer) -> None:
    client = HubClient(
        hub.url, API_KEY, gateway_name="Interop-GW", version="1.1.0-rc1", contact="qa@test"
    )
    result = client.register()
    assert result.ok
    assert hub.state.registrations == [
        {"name": "Interop-GW", "version": "1.1.0-rc1", "contact": "qa@test"}
    ]


def test_registration_rejects_wrong_api_key(hub: _StubServer) -> None:
    client = HubClient(hub.url, "wrong-key", "Bad-GW", "0.0.0")
    result = client.register()
    assert not result.ok  # 401 → immediate failure (no retry on non-5xx)
    assert hub.state.registrations == []


# ── POST /events ────────────────────────────────────────────────────────


def test_event_streamer_delivers_to_stub(hub: _StubServer) -> None:
    streamer = HubEventStreamer(hub.url, API_KEY, "Interop-GW", max_batch_size=4)
    streamer.start()
    try:
        for i in range(6):
            streamer.feed("STUDY_RECEIVED", {"study_uid": f"1.2.3.4.{i}"})
        deadline = time.monotonic() + 15.0
        while hub.state.events_delivered < 6 and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        streamer.stop()
    assert hub.state.events_delivered == 6
    batch = hub.state.event_batches[0]
    assert batch["gateway"] == "Interop-GW"
    assert all(set(e) == {"event", "detail", "ts"} for e in batch["events"])


# ── POST /anchor + offline verification (the M4 loop, end to end) ───────


def _anchor_heads(tmp_path: Path, hub: _StubServer, heads: list[str]) -> Path:
    signed = tmp_path / "audit-heads-signed.jsonl"
    anchorer = SignedHeadAnchorer(
        hub.url,
        API_KEY,
        tmp_path / "audit-heads.txt",
        signed,
        verify_key=hub.public_key_b64,
        gateway_name="Interop-GW",
    )
    anchorer.start()
    try:
        for head in heads:
            anchorer.anchor(head)
        anchorer.flush(timeout=15.0)
    finally:
        anchorer.stop()
    return signed


def test_signed_anchor_verifies_offline(tmp_path: Path, hub: _StubServer) -> None:
    heads = ["a" * 64, "b" * 64]
    signed = _anchor_heads(tmp_path, hub, heads)

    lines = [json.loads(line) for line in signed.read_text().splitlines()]
    assert [entry["head"] for entry in lines] == heads
    # The signature must be raw-Ed25519 (64 bytes) over the head's UTF-8 bytes.
    sig = base64.b64decode(lines[0]["signature"])
    hub.signing_key.public_key().verify(sig, heads[0].encode("utf-8"))

    ok, errors = verify_anchor_signatures(signed, hub.public_key_b64)
    assert ok, errors

    # And the operator CLI path used on release day (rc-checklist K5).
    from scripts.verify_audit_anchors import main as verify_main

    rc = verify_main(["--anchors", str(signed), "--public-key-b64", hub.public_key_b64])
    assert rc == 0


def test_tampered_anchor_fails_verification(tmp_path: Path, hub: _StubServer) -> None:
    signed = _anchor_heads(tmp_path, hub, ["c" * 64])
    entry = json.loads(signed.read_text().splitlines()[0])
    forged = dict(entry, head="d" * 64)  # attacker rewrites the chain head
    signed.write_text(json.dumps(forged) + "\n")

    ok, errors = verify_anchor_signatures(signed, hub.public_key_b64)
    assert not ok
    assert errors[0].reason == "signature does not verify"

    from scripts.verify_audit_anchors import main as verify_main

    assert verify_main(["--anchors", str(signed), "--public-key-b64", hub.public_key_b64]) == 1


def test_anchor_degrades_when_hub_rejects_auth(tmp_path: Path, hub: _StubServer) -> None:
    """US-10 isolation: 401 from the hub keeps the head file-anchored only."""
    signed = tmp_path / "audit-heads-signed.jsonl"
    anchorer = SignedHeadAnchorer(
        hub.url,
        "wrong-key",
        tmp_path / "audit-heads.txt",
        signed,
        gateway_name="Interop-GW",
        max_retries=1,  # one retry then drop — keeps the test fast
    )
    anchorer.start()
    try:
        anchorer.anchor("e" * 64)
        anchorer.flush(timeout=15.0)
    finally:
        anchorer.stop()
    assert not signed.exists()  # never signed
    file_anchor = (tmp_path / "audit-heads.txt").read_text()
    assert "e" * 64 in file_anchor  # integrity anchor still written
