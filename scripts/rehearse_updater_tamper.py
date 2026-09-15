#!/usr/bin/env python3
"""A4 (release-runbook §3): updater tamper-rejection rehearsal — no Windows needed.

Exercises the *production* code path for headless deployments end-to-end over
loopback HTTP, driving ``mercure_gateway.update.Updater`` against the real
signed deb artifact built by ``scripts/rehearse_signed_build.sh``:

  0. keypair: a fresh Ed25519 anchor stands in for the headless operator's
     release key — the flat manifest path consumes *raw* base64 Ed25519
     signatures over the archive bytes (see update.py "Signature scheme");
     the desktop path's minisign .sig + compiled-in Tauri pubkey is a
     different format, verified in the §3 desktop UAT leg instead.
  1. serve ``latest.json`` (flat {version,url,signature,checksum_sha256}) +
     the deb bytes from one local HTTP server;
  2. genuine control — ``check_update`` sees the update, ``apply_update``
     downloads + verifies + stages it (proves the rehearsal is live, not
     vacuously rejecting);
  3. tamper case — flip one bit of the signature in the served manifest
     (still a well-formed 64-byte Ed25519 signature): ``apply_update`` must
     refuse and stage nothing (ADR-0006 fail-closed).

Usage (from the repo root, after a signed local build exists):

    uv run python scripts/rehearse_updater_tamper.py

Exits 0 only if the accept case AND the reject case both behave as expected.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import json
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from mercure_gateway import __version__ as CURRENT_VERSION  # noqa: E402
from mercure_gateway.update import Updater  # noqa: E402


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args: object) -> None:  # keep the rehearsal record clean
        pass


def main() -> int:
    deb_candidates = sorted(
        REPO.glob("src-tauri/target/release/bundle/deb/*.deb"),
        key=lambda p: p.stat().st_mtime,
    )
    if not deb_candidates:
        raise SystemExit(
            "no signed deb found under src-tauri/target/release/bundle/deb/ — "
            "run scripts/rehearse_signed_build.sh first"
        )
    deb = deb_candidates[-1]

    private_key = Ed25519PrivateKey.generate()
    pubkey_b64 = base64.b64encode(
        private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ).decode()
    archive = deb.read_bytes()

    serve_dir = REPO / "build" / "updater-rehearsal"
    serve_dir.mkdir(parents=True, exist_ok=True)
    (serve_dir / deb.name).write_bytes(archive)

    def serve(signed: str, manifest_name: str, port: int) -> None:
        # Absolute URL: Updater.apply_update fetches manifest.url verbatim.
        manifest = {
            "version": "99.99.99",
            "url": f"http://127.0.0.1:{port}/{deb.name}",
            "signature": signed,
            "checksum_sha256": hashlib.sha256(archive).hexdigest(),
        }
        (serve_dir / manifest_name).write_text(json.dumps(manifest))

    def start_server() -> ThreadingHTTPServer:
        handler = functools.partial(_QuietHandler, directory=str(serve_dir))
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return httpd

    genuine_sig = base64.b64encode(private_key.sign(archive)).decode()
    raw_sig = base64.b64decode(genuine_sig)
    tampered_sig = base64.b64encode(bytes([raw_sig[0] ^ 0x01]) + raw_sig[1:]).decode()

    failures = 0

    # ── case 1: genuine signature must be accepted and staged ──────────────
    httpd = start_server()
    port = httpd.server_address[1]
    serve(genuine_sig, "latest.json", port)
    try:
        updater = Updater(
            update_url=f"http://127.0.0.1:{port}/latest.json",
            current_version=CURRENT_VERSION,
            public_key=pubkey_b64,
            timeout_sec=120,
        )
        check = updater.check_update()
        apply = (
            updater.apply_update(check.manifest)
            if check.available and check.manifest
            else None
        )
        ok = bool(check.available and apply and apply.ok and updater.pending_update)
        print(
            f"{'PASS' if ok else 'FAIL':4} genuine  → "
            f"check.available={check.available} apply.ok={getattr(apply, 'ok', None)} "
            f"staged={updater.pending_update is not None} "
            f"error={getattr(apply, 'error', None) or check.error!r}"
        )
        failures += 0 if ok else 1
    finally:
        httpd.shutdown()

    # ── case 2: bit-flipped signature must be refused, nothing staged ──────
    httpd = start_server()
    port = httpd.server_address[1]
    serve(tampered_sig, "latest.json", port)
    try:
        updater = Updater(
            update_url=f"http://127.0.0.1:{port}/latest.json",
            current_version=CURRENT_VERSION,
            public_key=pubkey_b64,
            timeout_sec=120,
        )
        check = updater.check_update()
        apply = (
            updater.apply_update(check.manifest)
            if check.available and check.manifest
            else None
        )
        ok = bool(
            check.available
            and apply
            and not apply.ok
            and updater.pending_update is None
            and apply.error
            and "signature" in apply.error
        )
        print(
            f"{'PASS' if ok else 'FAIL':4} tampered → "
            f"apply.ok={getattr(apply, 'ok', None)} "
            f"staged={updater.pending_update is not None} error={getattr(apply, 'error', None)!r}"
        )
        failures += 0 if ok else 1
    finally:
        httpd.shutdown()

    if failures:
        print("REHEARSAL FAILED — updater did not behave as ADR-0006 requires")
        return 1
    print(
        "REHEARSAL PASS — genuine signature accepted (control), tampered signature "
        "refused with nothing staged (release-runbook §3 / rc-checklist §3 auto-update, "
        "Python/headless half; the Tauri desktop half is the §3 clean-VM leg)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
