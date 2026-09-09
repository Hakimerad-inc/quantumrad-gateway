# Hub Anchor API — `POST /anchor` (review M4 signed heads)

Contract between the gateway's audit anchorer (`mercure_gateway.audit.anchoring`)
and the mercure hub bookkeeper's signing endpoint. Complements the existing
`POST /events` stream (S08-T2): events are the per-append stream; anchors are
the periodic integrity proofs over the audit hash chain.

## Request

```
POST {bookkeeper_url}/anchor
Authorization: Bearer {audit.hub_reporting.api_key}
Content-Type: application/json

{
  "gateway": "<appliance_name>",
  "head": "<64-char lowercase hex SHA-256 chain head>",
  "ts": "<ISO-8601 UTC timestamp from the gateway>"
}
```

## Response (2xx)

```
200 OK
Content-Type: application/json

{ "signature": "<base64 (raw 64-byte) Ed25519 signature>" }
```

The signature is computed over the **exact head string's UTF-8 bytes** (the
64-char hex string, nothing else). The signing private key exists only on the
hub; the gateway stores `{head, ts, signature}` in
`~/.local/share/mercure-gateway/audit-heads-signed.jsonl` and never sees the
key.

Errors: `401/403` for auth problems; any `4xx/5xx` (or transport failure) is
simply retried by the gateway with exponential backoff (0.2 s → 30 s, capped
at 5 attempts), after which the head remains file-anchored only and the
condition is logged. The gateway's audit append is **never** blocked by this
endpoint (US-10 isolation) — a downed hub degrades integrity-only anchoring,
exactly as when `anchor_public_key` is unset.

## Offline verification

The gateway side ships `scripts/verify_audit_anchors.py`; the hub's public key
is configured in `audit.hub_reporting.anchor_public_key` (also used to
self-verify). Signature rules match the gateway's update verification
(`mercure_gateway.update.load_ed25519_public_key` accepts PEM or raw base64).
