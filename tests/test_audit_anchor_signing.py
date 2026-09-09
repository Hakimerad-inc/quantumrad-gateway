"""Hub-signed audit head anchors (review M4 remainder).

The anchor file gives *integrity*, not *authenticity*: anyone who can rewrite
the spool DB can also rewrite ``audit-heads.txt``.  The fix (per decision) is a
**hub-held signing service**: the gateway POSTs each chain head to the
bookkeeper's ``POST /anchor`` endpoint, the hub signs the head with its Ed25519
private key, and the gateway stores ``{head, ts, signature}`` in a local JSONL
file.  The gateway never holds a signing key; ``verify_anchor_signatures()``
re-verifies every stored signature offline against the hub's public key.

Isolation (US-10): like :class:`~mercure_gateway.hub_events.HubEventStreamer`,
posting never blocks the caller — the file anchor is written synchronously
(cheap, local) and the network round-trip happens on a bounded daemon queue
with requeue-at-head + exponential backoff.
"""

from __future__ import annotations

import base64
import json
import logging
import threading
import time
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mercure_gateway.audit.anchoring import SignedHeadAnchorer, verify_anchor_signatures

# Test hub keypair — the gateway only ever sees the public half.
_HUB_KEY = Ed25519PrivateKey.generate()
_HUB_PUB_B64 = base64.b64encode(
    _HUB_KEY.public_key().public_bytes_raw()  # type: ignore[attr-defined]
).decode("ascii")


class FakeHub:
    """Hand-written stand-in for the bookkeeper's ``POST /anchor``.

    Mimics ``requests.post``: ``(url, json=..., headers=..., timeout=...)``.
    The signature it returns is what the real hub produces: base64 raw Ed25519
    over the head hex string's UTF-8 bytes.
    """

    def __init__(
        self, *, fail: int = 0, delay: float = 0.0, key: Ed25519PrivateKey = _HUB_KEY
    ) -> None:
        self.requests: list[dict] = []
        self.fail = fail  # number of initial requests to reject
        self.delay = delay
        self.key = key
        self.lock = threading.Lock()

    def __call__(self, url: str, json: dict | None = None, **_kw: object) -> FakeHubResponse:
        if self.delay:
            time.sleep(self.delay)
        with self.lock:
            self.requests.append({"url": url, "json": json})
            if self.fail > 0:
                self.fail -= 1
                return FakeHubResponse(503, None)
        head = str(json["head"])
        sig = self.key.sign(head.encode("utf-8"))
        return FakeHubResponse(200, {"signature": base64.b64encode(sig).decode("ascii")})


class FakeHubResponse:
    """Minimal ``requests.Response`` stand-in (status_code + json())."""

    def __init__(self, status_code: int, payload: dict | None) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        assert self._payload is not None
        return self._payload


def _make_anchorer(tmp_path: Path, hub: FakeHub, **kw: object) -> SignedHeadAnchorer:
    anchorer = SignedHeadAnchorer(
        "https://hub.example",
        "secret-key",
        tmp_path / "audit-heads.txt",
        tmp_path / "audit-heads-signed.jsonl",
        verify_key=_HUB_PUB_B64,
        post=hub,
        **kw,  # type: ignore[arg-type]
    )
    anchorer.start()
    return anchorer


def test_anchor_writes_file_and_signed_entry(tmp_path: Path) -> None:
    hub = FakeHub()
    anchorer = _make_anchorer(tmp_path, hub)
    anchorer.anchor("a" * 64)
    anchorer.flush(timeout=5)

    heads = (tmp_path / "audit-heads.txt").read_text(encoding="utf-8").splitlines()
    assert heads == ["a" * 64]
    entries = [json.loads(line) for line in
               (tmp_path / "audit-heads-signed.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(entries) == 1
    assert entries[0]["head"] == "a" * 64
    assert "signature" in entries[0] and "ts" in entries[0]


def test_signature_round_trip(tmp_path: Path) -> None:
    hub = FakeHub()
    anchorer = _make_anchorer(tmp_path, hub)
    anchorer.anchor("b" * 64)
    anchorer.flush(timeout=5)

    ok, errors = verify_anchor_signatures(
        tmp_path / "audit-heads-signed.jsonl", _HUB_PUB_B64
    )
    assert ok is True
    assert errors == []


def test_verify_detects_tampered_head(tmp_path: Path) -> None:
    hub = FakeHub()
    anchorer = _make_anchorer(tmp_path, hub)
    anchorer.anchor("c" * 64)
    anchorer.flush(timeout=5)

    path = tmp_path / "audit-heads-signed.jsonl"
    tampered = path.read_text(encoding="utf-8").replace("c" * 64, "d" * 64)
    path.write_text(tampered, encoding="utf-8")

    ok, errors = verify_anchor_signatures(path, _HUB_PUB_B64)
    assert ok is False
    assert len(errors) == 1
    assert errors[0].line_no == 1


def test_verify_rejects_foreign_key(tmp_path: Path) -> None:
    hub = FakeHub()
    anchorer = _make_anchorer(tmp_path, hub)
    anchorer.anchor("e" * 64)
    anchorer.flush(timeout=5)

    other_key = base64.b64encode(
        Ed25519PrivateKey.generate().public_key().public_bytes_raw()  # type: ignore[attr-defined]
    ).decode("ascii")
    ok, errors = verify_anchor_signatures(
        tmp_path / "audit-heads-signed.jsonl", other_key
    )
    assert ok is False
    assert len(errors) == 1


def test_verify_malformed_line_reported_not_raised(tmp_path: Path) -> None:
    path = tmp_path / "audit-heads-signed.jsonl"
    path.write_text(
        "not json\n"
        + json.dumps({"head": "f" * 64}) + "\n"  # no signature field
        + json.dumps({"head": "g" * 64, "signature": "!!!", "ts": "t"}) + "\n",
        encoding="utf-8",
    )
    ok, errors = verify_anchor_signatures(path, _HUB_PUB_B64)
    assert ok is False
    assert len(errors) == 3
    assert [e.line_no for e in errors] == [1, 2, 3]


def test_anchor_is_non_blocking_when_hub_is_slow(tmp_path: Path) -> None:
    hub = FakeHub(delay=2.0)
    anchorer = _make_anchorer(tmp_path, hub)
    start = time.monotonic()
    anchorer.anchor("1" * 64)
    elapsed = time.monotonic() - start
    assert elapsed < 0.5, f"anchor() blocked for {elapsed:.2f}s on a slow hub"
    anchorer.stop()


def test_falls_back_to_file_only_when_hub_down(tmp_path: Path, caplog) -> None:  # type: ignore[no-untyped-def]
    hub = FakeHub(fail=3)
    anchorer = _make_anchorer(tmp_path, hub, max_retries=2)
    with caplog.at_level(logging.WARNING, logger="mercure_gateway.audit.anchoring"):
        anchorer.anchor("2" * 64)
        anchorer.flush(timeout=10)
    # File anchor survives even though the hub never signed anything.
    heads = (tmp_path / "audit-heads.txt").read_text(encoding="utf-8").splitlines()
    assert heads == ["2" * 64]
    assert not (tmp_path / "audit-heads-signed.jsonl").exists()
    assert any("anchor" in r.message.lower() for r in caplog.records)
    anchorer.stop()


def test_queue_is_bounded_drop_oldest(tmp_path: Path) -> None:
    hub = FakeHub()
    anchorer = _make_anchorer(tmp_path, hub, max_queue_size=4)
    anchorer.start()
    for i in range(20):
        anchorer.anchor(f"{i:064x}")
    anchorer.flush(timeout=10)
    entries = [json.loads(line) for line in
               (tmp_path / "audit-heads-signed.jsonl").read_text(encoding="utf-8").splitlines()]
    # Bounded queue, drop-oldest: the worker races the producer, so more
    # than maxlen entries can be signed overall (each signed head frees a
    # slot for a later one) — but far fewer than the 20 produced, and the
    # newest head must always survive.
    assert len(entries) < 20
    heads_signed = {e["head"] for e in entries}
    assert f"{19:064x}" in heads_signed  # newest head always survives
    anchorer.stop()


def test_flush_and_stop_drain_worker(tmp_path: Path) -> None:
    hub = FakeHub()
    anchorer = _make_anchorer(tmp_path, hub)
    anchorer.start()
    for i in range(5):
        anchorer.anchor(f"{i:064x}")
    anchorer.stop()  # stop must drain pending heads
    entries = [json.loads(line) for line in
               (tmp_path / "audit-heads-signed.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(entries) == 5


def test_verify_plain_head_file(tmp_path: Path) -> None:
    """The unsigned anchor file (one hex hash per line) verifies trivially."""
    path = tmp_path / "audit-heads.txt"
    path.write_text("a" * 64 + "\n" + "b" * 64 + "\n", encoding="utf-8")
    ok, errors = verify_anchor_signatures(path, _HUB_PUB_B64)
    assert ok is True
    assert errors == []


# ── composition root wiring ──────────────────────────────────────────────


def test_wire_head_anchorer_file_only_without_key() -> None:
    """No hub key ⇒ plain file anchorer, no worker to stop."""
    import mercure_gateway.main as main_mod
    from mercure_gateway.audit import AuditLog
    from mercure_gateway.config import default_config
    from mercure_gateway.spool.db import mem_database

    audit = AuditLog(mem_database())
    cfg = default_config()
    assert main_mod._wire_head_anchorer(cfg, audit) is None
    assert audit._head_anchorer is not None


def test_wire_head_anchorer_signed_when_key_configured(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """hub enabled + anchor_public_key ⇒ SignedHeadAnchorer wired and started."""
    import base64

    import mercure_gateway.main as main_mod
    from mercure_gateway.audit import AuditLog
    from mercure_gateway.config import default_config
    from mercure_gateway.spool.db import mem_database

    key = Ed25519PrivateKey.generate()
    pub = base64.b64encode(key.public_key().public_bytes_raw()).decode("ascii")

    cfg = default_config()
    cfg.audit.hub_reporting.enabled = True
    cfg.audit.hub_reporting.bookkeeper_url = "https://hub.example"
    cfg.audit.hub_reporting.api_key = "k"
    cfg.audit.hub_reporting.anchor_public_key = pub

    audit = AuditLog(mem_database())
    monkeypatch.setattr(
        main_mod, "_audit_anchor_path", lambda _c: tmp_path / "heads.txt"
    )
    monkeypatch.setattr(
        main_mod, "_signed_anchor_path", lambda _c: tmp_path / "signed.jsonl"
    )

    # Intercept the worker's network path so no real POST can happen.
    from mercure_gateway.audit import anchoring as anchoring_mod

    monkeypatch.setattr(
        anchoring_mod, "requests", type("R", (), {"post": staticmethod(lambda *a, **k: None)})
    )

    anchorer = main_mod._wire_head_anchorer(cfg, audit)
    try:
        assert anchorer is not None
        assert audit._head_anchorer is not None
        # The audit anchorer must be the anchorer's own anchor method.
        audit._anchor_head(1)
        anchorer.flush(timeout=5)
        assert (tmp_path / "heads.txt").exists()
    finally:
        anchorer.stop()
