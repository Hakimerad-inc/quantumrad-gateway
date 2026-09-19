"""Scheduled verification of hub-signed anchors (review P0-10).

``verify_anchor_signatures`` detected a rewritten audit chain — but nothing in
production ever called it.  The chain's *internal* hash replay
(``/api/audit/verify``) cannot see a whole-chain rewrite: an attacker who
rewrites the database recomputes those hashes.  Only the hub-held Ed25519
signatures can, so the check has to run without an operator remembering to ask.

These tests cover the timer that makes it run, plus the failure callback that
carries the finding into the audit stream an unmanned box is being watched by.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mercure_gateway.audit.anchoring import (
    AnchorError,
    AnchorVerification,
    AnchorVerifier,
    SignedHeadAnchorer,
)

# Test hub keypair — the gateway only ever sees the public half.
_HUB_KEY = Ed25519PrivateKey.generate()
_HUB_PUB_B64 = base64.b64encode(
    _HUB_KEY.public_key().public_bytes_raw()  # type: ignore[attr-defined]
).decode("ascii")


class _SigningHub:
    """Signs each head exactly as the real bookkeeper does (POST /anchor)."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, url: str, json: dict | None = None, **_kw: object) -> object:
        self.calls += 1
        head = str(json["head"])
        sig = _HUB_KEY.sign(head.encode("utf-8"))

        class _Resp:
            status_code = 200

            def json(self) -> dict[str, str]:
                return {"signature": base64.b64encode(sig).decode("ascii")}

        return _Resp()


def _build_signed_anchor(tmp_path: Path, heads: list[str]) -> Path:
    """Produce a real signed-anchor JSONL via the anchorer itself."""
    hub = _SigningHub()
    anchorer = SignedHeadAnchorer(
        "https://hub.example",
        "secret-key",
        tmp_path / "audit-heads.txt",
        tmp_path / "audit-heads-signed.jsonl",
        verify_key=_HUB_PUB_B64,
        post=hub,
    )
    anchorer.start()
    for head in heads:
        anchorer.anchor(head)
    anchorer.flush(timeout=5)
    anchorer.stop()
    return tmp_path / "audit-heads-signed.jsonl"


def _wait_until(predicate: object, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():  # type: ignore[operator]
            return
        time.sleep(0.02)
    raise AssertionError(f"condition not met within {timeout}s")


# ── the timer ──────────────────────────────────────────────────────────────


def test_a_valid_anchor_verifies_on_demand(tmp_path: Path) -> None:
    path = _build_signed_anchor(tmp_path, ["a" * 64])
    verifier = AnchorVerifier(path, _HUB_PUB_B64)

    result = verifier.verify_now()

    assert result.ok is True
    assert result.errors == ()


def test_an_absent_anchor_file_is_not_a_failure(tmp_path: Path) -> None:
    """A gateway that has appended no chain head yet has nothing to verify."""
    verifier = AnchorVerifier(tmp_path / "nope.jsonl", _HUB_PUB_B64)

    result = verifier.verify_now()

    assert result.ok is True
    assert result.file_absent is True


def test_the_timer_detects_a_chain_rewritten_after_boot(tmp_path: Path) -> None:
    """The finding P0-10 is about: a tamper that happens later is still caught.

    The signature was valid when the gateway stored it; the verifier has to
    re-read the file on its cadence, not trust a boot-time pass.
    """
    path = _build_signed_anchor(tmp_path, ["c" * 64])
    failures: list[AnchorVerification] = []
    verifier = AnchorVerifier(
        path,
        _HUB_PUB_B64,
        interval_sec=0.05,
        initial_delay_sec=0.0,
        on_failure=failures.append,
    )

    verifier.start()
    try:
        _wait_until(lambda: verifier.last_result is not None)
        assert verifier.last_result is not None and verifier.last_result.ok

        # Rewrite the anchored head — the attacker recomputes nothing here, so
        # the stored signature is now over a head that no longer exists.
        path.write_text(
            path.read_text(encoding="utf-8").replace("c" * 64, "d" * 64),
            encoding="utf-8",
        )

        _wait_until(lambda: not verifier.last_result.ok)  # type: ignore[union-attr]
        _wait_until(lambda: bool(failures))
    finally:
        verifier.stop()

    assert verifier.failures_total >= 1
    assert all(isinstance(e, AnchorError) for e in failures[0].errors)
    assert failures[0].errors[0].reason == "signature does not verify"


def test_a_corrupt_anchor_file_fails_loudly_not_fatal(tmp_path: Path) -> None:
    """Garbage on disk is a finding, never a crash that silences the timer."""
    path = tmp_path / "signed.jsonl"
    path.write_text("{not json at all\n", encoding="utf-8")
    verifier = AnchorVerifier(path, _HUB_PUB_B64)

    result = verifier.verify_now()

    assert result.ok is False
    assert verifier.failures_total >= 1


def test_stop_joins_the_timer_thread(tmp_path: Path) -> None:
    verifier = AnchorVerifier(tmp_path / "nope.jsonl", _HUB_PUB_B64, interval_sec=0.05)
    verifier.start()
    verifier.stop()

    assert verifier._thread is None  # noqa: SLF001 — the lifecycle contract


def test_a_failure_callback_that_raises_does_not_kill_the_timer(
    tmp_path: Path, caplog: Any
) -> None:
    """The reporting path must not become a second way to lose the finding."""
    path = tmp_path / "signed.jsonl"
    path.write_text("{garbage\n", encoding="utf-8")

    def explode(_result: AnchorVerification) -> None:
        raise RuntimeError("downstream sink is broken")

    verifier = AnchorVerifier(
        path,
        _HUB_PUB_B64,
        interval_sec=0.05,
        initial_delay_sec=0.0,
        on_failure=explode,
    )
    verifier.start()
    try:
        _wait_until(lambda: verifier.failures_total >= 2)
    finally:
        verifier.stop()

    # The timer kept running past the first broken callback.
    assert verifier.failures_total >= 2


# ── composition root wiring ────────────────────────────────────────────────


def test_start_anchor_verification_is_a_noop_without_the_hub_key(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """No anchor_public_key ⇒ unsigned anchoring ⇒ nothing to verify.

    Chain *integrity* is already covered by /api/audit/verify in that mode;
    only hub-held signatures give authenticity.
    """
    import mercure_gateway.main as main_mod
    from mercure_gateway.audit import AuditLog
    from mercure_gateway.config import default_config
    from mercure_gateway.spool.db import mem_database

    audit = AuditLog(mem_database())
    cfg = default_config()
    cfg.audit.hub_reporting.enabled = True
    cfg.audit.hub_reporting.bookkeeper_url = "https://hub.example"
    cfg.audit.hub_reporting.api_key = "k"

    assert main_mod._start_anchor_verification(cfg, audit) is None


def test_start_anchor_verification_schedules_the_signed_file(
    tmp_path: Path, monkeypatch: Any
) -> None:
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
        main_mod, "_signed_anchor_path", lambda _c: tmp_path / "signed.jsonl"
    )

    verifier = main_mod._start_anchor_verification(cfg, audit)
    try:
        assert verifier is not None
        # The composition root aims the verifier at the file the signed
        # anchorer actually writes — a stale path would verify nothing.
        verifier.verify_now()
        assert verifier.last_result is not None
        assert verifier.last_result.file_absent is True
    finally:
        assert verifier is not None
        verifier.stop()


def test_a_failed_verification_becomes_an_audit_event(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The finding reaches the hub feed, not just a local log line.

    A tampered audit log cannot suppress the copy of the finding that already
    shipped, so recording it in the chain (which the streamer then ships) is
    what makes the check worth running on an unmanned box.
    """
    import mercure_gateway.main as main_mod
    from mercure_gateway.audit import AuditLog
    from mercure_gateway.config import default_config
    from mercure_gateway.spool.db import mem_database

    database = mem_database()
    audit = AuditLog(database)
    (tmp_path / "signed.jsonl").write_text("{garbage\n", encoding="utf-8")

    key = Ed25519PrivateKey.generate()
    pub = base64.b64encode(key.public_key().public_bytes_raw()).decode("ascii")
    cfg = default_config()
    cfg.audit.hub_reporting.enabled = True
    cfg.audit.hub_reporting.bookkeeper_url = "https://hub.example"
    cfg.audit.hub_reporting.anchor_public_key = pub

    monkeypatch.setattr(
        main_mod, "_signed_anchor_path", lambda _c: tmp_path / "signed.jsonl"
    )

    verifier = main_mod._start_anchor_verification(cfg, audit)
    try:
        assert verifier is not None
        verifier.verify_now()
    finally:
        verifier.stop()

    rows = database.list_audit_events()
    events = [r["event"] for r in rows]
    assert "AUDIT_ANCHOR_FAILED" in events
    detail = json.loads(
        next(r["detail"] for r in rows if r["event"] == "AUDIT_ANCHOR_FAILED")
    )
    assert detail["errors"]
