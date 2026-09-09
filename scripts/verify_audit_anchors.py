#!/usr/bin/env python
"""Offline verification of hub-signed audit head anchors (review M4).

Operator workflow: copy ``audit-heads-signed.jsonl`` (and, optionally, the
plain ``audit-heads.txt``) off the gateway device, then run this script
anywhere with the hub's Ed25519 public key. Every stored signature must
verify over the exact head string; any failure is a tampering indicator.

Usage:
    uv run python scripts/verify_audit_anchors.py \
        --anchors ~/.local/share/mercure-gateway/audit-heads-signed.jsonl \
        --public-key hub-anchor.pub            # PEM or raw-base64 file
    # or pass the key inline:
        --public-key-b64 "MCowBQYDK2VwAyEA..."
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from mercure_gateway.audit.anchoring import verify_anchor_signatures

__all__ = ["main"]


def _read_key(path_or_b64: str) -> str:
    """Accept a file path (PEM or base64 content) or an inline base64 string."""
    candidate = Path(path_or_b64)
    if candidate.exists():
        return candidate.read_text(encoding="utf-8")
    return path_or_b64


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--anchors",
        required=True,
        help="path to audit-heads-signed.jsonl (or the plain audit-heads.txt)",
    )
    key_group = parser.add_mutually_exclusive_group(required=True)
    key_group.add_argument("--public-key", help="path to a PEM or raw-base64 public key file")
    key_group.add_argument("--public-key-b64", help="inline raw-base64 Ed25519 public key")
    args = parser.parse_args(argv)

    key = (
        _read_key(args.public_key)
        if args.public_key
        else (args.public_key_b64 or "")
    )
    ok, errors = verify_anchor_signatures(Path(args.anchors), key)
    if errors and errors[0].reason == "no verification key configured":
        print("error: could not parse the given public key", file=sys.stderr)
        return 2
    if ok:
        print(f"OK: all anchors in {args.anchors} verify against the hub key")
        return 0
    print(f"FAIL: {len(errors)} anchor line(s) did not verify:")
    for err in errors:
        print(f"  line {err.line_no}: {err.reason}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
