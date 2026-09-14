# ADR-0007: Web Admin Panel Transport Posture — Loopback Default, Refusal Boundary, Optional TLS

**Status:** Accepted
**Date:** 2026-09-14
**Deciders:** Product + Engineering (clinical-deployment review, D3/D3b)
**Relates to:** PRD §6.3, refinement §7, admin-guide §Authentication / §Transport security

## Context

The web admin panel (FastAPI + SPA, ADR-0002) exposes PHI, credential
management, and receiver/forwarder start/stop. In a clinical deployment the
transport question is mandatory, and two issues surfaced during the
production-readiness review:

1. **A documented control that did not exist.** The admin guide, the
   `web/auth.py` module docstring, and a `web/__init__.py` comment all claimed
   the gateway *refuses* to bind to a non-loopback address while
   `web_ui.auth_enabled` is false. `main.py` actually logged a warning and
   bound anyway — so `host=0.0.0.0` + default auth-off shipped an
   unauthenticated PHI API to the LAN. The `require_auth` no-op-when-auth-off
   design *depends* on the refusal; without it the "loopback-only" assumption
   was decorative. (Fixed as D3b before this ADR: hard refusal now.)

2. **Plain HTTP with an unconditional HSTS header.** Browsers ignore HSTS on
   `http://` responses; sending it there advertised a protection that cannot
   exist, and the CSP/CSRF posture documented "shared machines" use without a
   transport story.

Options considered for the panel's transport:

1. **Loopback-only + SSH tunnel for remote admin; optional operator-supplied
   TLS.** The panel binds `127.0.0.1` by default (unchanged). Remote admin is
   `ssh -L 8443:127.0.0.1:8080` on the gateway box — TLS and authentication
   are the SSH layer's job, and the site's existing SSH hardening/bastion
   policy applies. For sites that must serve the panel on the network,
   `web_ui.tls_cert_file`/`tls_key_file` give uvicorn an operator-managed
   certificate (hospital PKI; the gateway does not mint or ACME certs).
2. **Built-in self-signed certificate.** Removes the "cleartext on LAN"
   failure but trains operators to click through browser warnings — a
   worse security culture in a clinic than an honest loopback default.
3. **Mandatory TLS everywhere.** Breaks the single-user desktop case (the
   Tauri shell loads the sidecar over loopback HTTP; every dev/test harness
   would need certs) for no benefit at the loopback boundary.

## Decision

**Option 1: loopback default with SSH-tunnel remote admin, plus optional
operator-supplied TLS, with the non-loopback refusal as the hard boundary.**

Concretely (all landed with this ADR):

- **Refusal boundary (D3b, done first):** `main._enforce_bind_security`
  raises `SystemExit` when the panel would bind a non-loopback address
  (`127.0.0.1` / `localhost` / `::1` are loopback) with `auth_enabled=false`.
  `MERCURE_GATEWAY_ALLOW_INSECURE_BIND=1` downgrades the refusal to a loud
  warning for dev rigs and TLS-terminating reverse proxies. Escape-hatch use
  on a PHI-serving box is an audit finding by definition.
- **Optional TLS (D3a):** `WebUIConfig.tls_cert_file` + `tls_key_file`,
  validated as a pair (half a pair is a config error — never a silent
  HTTP downgrade). Wired to `uvicorn.run(ssl_certfile=…, ssl_keyfile=…)`.
- **HSTS honesty:** the `Strict-Transport-Security` header is emitted only
  when the ASGI scope's scheme is `https`. Plain-HTTP responses no longer
  claim it. CSP, `X-Frame-Options`, `X-Content-Type-Options`, and the CSRF
  origin check apply on every response regardless.
- **Tunnel pattern documented** in the admin guide §Transport security as
  the recommended remote-admin route; the refusal message itself names the
  two compliant alternatives (enable auth / TLS, or bind loopback).

Consequences:

- With TLS configured, the panel is legitimately reachable on the network —
  the refusal boundary still applies (auth must be on, or TLS+auth, or the
  explicit hatch), so PHI never crosses an unauthenticated cleartext socket
  by accident.
- The `web/auth.py` docstring's safety argument ("API open is safe because
  main refuses non-loopback in that mode") is now actually true and is
  covered by tests in `tests/test_web_security.py`.
- Auto-update (ADR-0006) is unaffected: it is an outbound HTTPS client to
  the GitHub release endpoint, not the panel server.
- The metrics scrape target (D1) sits behind the same boundary: loopback is
  scrape-friendly; an authenticated panel requires the Prometheus job to
  carry the Bearer session token.

## Rejected

- Self-signed baked-in certs (warning-click culture); mandatory TLS (breaks
  the desktop/single-user default); warn-only binding (the status quo D3b
  replaces — the documented-control gap was its own disqualifier).
