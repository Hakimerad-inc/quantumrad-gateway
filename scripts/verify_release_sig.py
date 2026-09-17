#!/usr/bin/env python
"""Offline verification of a published release artifact (runbook §3).

Complements eyeballing the GitHub release page: this actually proves the
Ed25519 signature over the downloaded installer, against the updater public
key in the runbook §0.1 custody record. Use it on anything pulled from a
release before installing it, and on the CI-built sidecars after a cut.

The ``.sig`` sidecars emitted by the Tauri CLI are minisign-format blobs
(base64 of an ``untrusted comment`` block). The signed payload is
``blake2b-512`` of the artifact (minisign prehash), so a plain raw Ed25519
check over the file bytes will fail — this script handles the format.

Usage:
    uv run python scripts/verify_release_sig.py \
        --artifact QuantumRAD-Gateway_1.1.0-rc2_amd64.deb \
        --signature  QuantumRAD-Gateway_1.1.0-rc2_amd64.deb.sig \
        --public-key ~/.tauri/mercure-gateway.key.pub

The key file is the minisign .pub (``untrusted comment: minisign public key``
+ base64). Exits 0 on a good signature, 1 on any mismatch or malformed input.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import sys
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

__all__ = ["main"]

# minisign binary layout: sig_algorithm (2) || keynum (8) || payload (32 or 64)
_PREFIX_LEN = 10
_KEY_LEN = 32
_SIG_LEN = 64
_DIGEST_SIZE = 64  # blake2b-512


def _minisign_block(path: Path) -> list[str]:
    """Decode a base64-wrapped minisign block into its text lines."""
    raw = path.read_bytes().decode().strip()
    return base64.b64decode(raw).decode().splitlines()


def _payload(line: str, want_len: int, what: str) -> bytes:
    """Extract and length-check a minisign payload line."""
    decoded = base64.b64decode(line)
    if len(decoded) != _PREFIX_LEN + want_len:
        raise ValueError(f"{what} is {_PREFIX_LEN + want_len}+{len(decoded)} bytes, malformed")
    return decoded


def verify(artifact: Path, signature: Path, public_key: Path) -> bool:
    """Return True iff *signature* is a valid minisign prehash signature.

    Raises ``ValueError`` on malformed input (a malformed file is a loud
    failure, not a silent rejection); returns ``False`` only on a real
    signature mismatch, which is an attack signal worth reporting plainly.
    """
    pub_lines = _minisign_block(public_key)
    sig_lines = _minisign_block(signature)
    if len(pub_lines) < 2 or len(sig_lines) < 4:
        raise ValueError("minisign block is missing its payload lines")

    pub_pl = _payload(pub_lines[1], _KEY_LEN, "public key")
    sig_pl = _payload(sig_lines[1], _SIG_LEN, "signature")

    keynum_pub, key = pub_pl[2:_PREFIX_LEN], pub_pl[_PREFIX_LEN:]
    keynum_sig, signature_bytes = sig_pl[2:_PREFIX_LEN], sig_pl[_PREFIX_LEN:]
    if keynum_pub != keynum_sig:
        raise ValueError(
            f"signature keynum {keynum_sig.hex()} != pubkey keynum {keynum_pub.hex()}"
        )

    public = Ed25519PublicKey.from_public_bytes(key)
    digest = hashlib.blake2b(artifact.read_bytes(), digest_size=_DIGEST_SIZE).digest()
    try:
        public.verify(signature_bytes, digest)
    except InvalidSignature:
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--artifact", required=True, type=Path, help="downloaded installer")
    parser.add_argument("--signature", required=True, type=Path, help="its .sig sidecar")
    parser.add_argument(
        "--public-key", required=True, type=Path, help="minisign .pub from runbook §0.1"
    )
    args = parser.parse_args(argv)

    for label, path in (
        ("artifact", args.artifact),
        ("signature", args.signature),
        ("public key", args.public_key),
    ):
        if not path.is_file():
            print(f"error: {label} not found: {path}", file=sys.stderr)
            return 1

    try:
        ok = verify(args.artifact, args.signature, args.public_key)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not ok:
        print(f"FAIL: {args.artifact} signature does NOT match the trusted key", file=sys.stderr)
        return 1

    print(f"OK: {args.artifact.name} verifies against the custody public key")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
