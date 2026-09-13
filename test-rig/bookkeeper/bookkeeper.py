#!/usr/bin/env python
"""Hub bookkeeper stub — mercure-hub stand-in for gateway interop (S01-T5).

Implements the three bookkeeper endpoints the gateway talks to, per
``docs/dev/hub-anchor-api.md`` (anchors) and the TD-19 ``Token`` auth scheme
used by ``hub_client`` / ``hub_events``:

* ``POST /register-gateway`` — one-time appliance registration (S08-T1).
* ``POST /events``           — audit-event stream, at-least-once (S08-T2).
* ``POST /anchor``           — Ed25519 signature over the audit chain head
  (review M4): signs the *exact head string's UTF-8 bytes* and returns
  ``{"signature": "<base64 raw 64-byte Ed25519>"}``.

Everything else is deliberately minimal: state lives in memory and is
inspected directly by the interop test (same process) or via the loopback-only
``GET /state`` debug endpoint when run standalone. The signing key is supplied
by the caller (test fixture) or, standalone, generated per-boot and printed to
stdout so an operator can paste it into ``audit.hub_reporting.anchor_public_key``.

Standalone run (next to the Orthanc rig, loopback-only per RSK-19):

    uv run python test-rig/bookkeeper/bookkeeper.py --port 8050
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

__all__ = ["BookkeeperState", "create_app", "main"]

_HEAD_RE = re.compile(r"^[0-9a-f]{64}$")
_API_KEY_ENV = "BOOKKEEPER_API_KEY"
_HOST = "127.0.0.1"  # loopback-only, matching the Orthanc rig (RSK-19)


class BookkeeperState:
    """In-memory record of what the gateway sent (test assertions read this)."""

    def __init__(self, api_key: str, signing_key: Ed25519PrivateKey) -> None:
        self.api_key = api_key
        self.signing_key = signing_key
        self.registrations: list[dict[str, Any]] = []
        self.event_batches: list[dict[str, Any]] = []
        self.anchor_requests: list[dict[str, Any]] = []
        self.auth_headers_seen: list[str] = []

    @property
    def public_key_b64(self) -> str:
        pub = self.signing_key.public_key()
        raw = pub.public_bytes_raw()
        return base64.b64encode(raw).decode("ascii")

    @property
    def events_delivered(self) -> int:
        return sum(len(b["events"]) for b in self.event_batches)


class RegisterBody(BaseModel):
    name: str = Field(min_length=1)
    version: str = ""
    contact: str = ""


class EventsBody(BaseModel):
    gateway: str = Field(min_length=1)
    events: list[dict[str, Any]]


class AnchorBody(BaseModel):
    gateway: str = Field(min_length=1)
    head: str = Field(min_length=64, max_length=64)
    ts: str = ""


def create_app(state: BookkeeperState) -> FastAPI:
    """Build the stub FastAPI app backed by *state*."""

    app = FastAPI(title="mercure hub bookkeeper stub")

    def require_token(request: Request) -> None:
        header = request.headers.get("Authorization", "")
        state.auth_headers_seen.append(header)
        expected = f"Token {state.api_key}"
        if header != expected:
            raise HTTPException(status_code=401, detail="invalid api key")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/state")
    def get_state() -> JSONResponse:  # loopback-only debug view
        return JSONResponse(
            {
                "registrations": state.registrations,
                "events_batches": len(state.event_batches),
                "events_delivered": state.events_delivered,
                "anchors_signed": len(state.anchor_requests),
                "anchor_public_key": state.public_key_b64,
            }
        )

    @app.post("/register-gateway", dependencies=[Depends(require_token)])
    def register_gateway(body: RegisterBody) -> dict[str, Any]:
        state.registrations.append(body.model_dump())
        return {"ok": True}

    @app.post("/events", dependencies=[Depends(require_token)])
    def post_events(body: EventsBody) -> dict[str, Any]:
        state.event_batches.append(body.model_dump())
        return {"ok": True, "received": len(body.events)}

    @app.post("/anchor", dependencies=[Depends(require_token)])
    def post_anchor(body: AnchorBody) -> dict[str, Any]:
        # Sign the exact head string's UTF-8 bytes — the contract in
        # docs/dev/hub-anchor-api.md. Any other bytes and the gateway's
        # offline verifier rejects the anchor.
        if not _HEAD_RE.match(body.head):
            raise HTTPException(status_code=422, detail="head must be 64 lowercase hex chars")
        sig = state.signing_key.sign(body.head.encode("utf-8"))
        state.anchor_requests.append(json.loads(body.model_dump_json()))
        return {"signature": base64.b64encode(sig).decode("ascii")}

    return app


def _load_or_generate_signing_key() -> Ed25519PrivateKey:
    pem_path = os.environ.get("BOOKKEEPER_SIGNING_KEY_PEM")
    if pem_path and os.path.exists(pem_path):
        with open(pem_path, "rb") as fh:
            key = serialization.load_pem_private_key(fh.read(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise SystemExit(f"{pem_path} is not an Ed25519 key")
        return key
    key = Ed25519PrivateKey.generate()
    if pem_path:
        with open(pem_path, "wb") as fh:
            fh.write(
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )
    return key


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8050)
    args = parser.parse_args(argv)

    api_key = os.environ.get(_API_KEY_ENV)
    if not api_key:
        print(f"error: {_API_KEY_ENV} must be set", file=sys.stderr)
        return 2
    import uvicorn

    state = BookkeeperState(api_key, _load_or_generate_signing_key())
    # Operators need the public half to configure anchor_public_key.
    print(f"hub anchor public key (base64): {state.public_key_b64}")
    uvicorn.run(create_app(state), host=_HOST, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
