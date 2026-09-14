# mercure Gateway — Admin Guide

## Overview

This guide covers configuration, security, retention, and troubleshooting for
administrators operating mercure Gateway. The gateway is a store-and-forward
DICOM appliance: modalities push studies to a local receiver; the gateway holds
them in a spool; a forwarder delivers them to configured destinations with
retry and exponential backoff.

## Supported platforms (PRD §13 Q8)

Tested and QA-matrixed (rc1-checklist §3): **Windows 10 x64**, **Windows 11
x64**, and **Ubuntu (current supported LTS)** — desktop installer and headless
systemd service. **Windows Server is documented-only**: the gateway has no
Server-specific validation, no service-mode testing on Server SKUs, and the
OS-keyring credential store is exercised only via Windows Credential Manager.
It is expected to work (same Win32 subsystem, same installer) but is not a
supported configuration for incident response purposes.

## Configuration

Configuration lives in `mercure-gateway.json` (next to the executable). The
web admin **Config** tab is the supported way to edit it; the file can also be
edited directly when the gateway is stopped.

### Key sections

| Section | What it controls |
|---------|------------------|
| `general` | Appliance name, locale, log level |
| `receiver` | AE title, listen port, max associations, compressed-syntax handling, auto-enqueue delay, AE allow-list |
| `destinations` | Forwarding targets (DICOM, DICOM-TLS, DICOMweb, SFTP, rsync, S3, Folder, XNAT) |
| `forwarding` | Concurrency and queue poll interval |
| `forwarding_rules` | Advanced routing rules (modality / patient / accession / description) |
| `reports` | Report retrieval: query source (DICOM, DICOMweb), poll interval, SLA |
| `storage` | Spool directory, capacity budget, retention window, disk-full behaviour |
| `audit` | Local audit log (encryption, PHI scope, retention, hub reporting) |
| `web_ui` | Admin panel bind host/port, authentication |
| `credentials` | Encrypted per-destination credential store |
| `usb_mode` | Portable USB variant settings (Sprint 10) |

### Import / Export

Use **Config → Export** to download the current configuration as JSON with all
secrets redacted (passwords and keys are replaced with `***`). **Import**
applies a previously exported file. Never edit secrets in an exported file and
re-import it as plaintext — use the encrypted credential store or the wizard.

## Security

### Authentication

By default the admin panel binds to `127.0.0.1` only and authentication is
disabled (single-user loopback). When the panel is reachable on a network, or
on shared machines:

1. Enable `web_ui.auth_enabled` and set a password hash via the **Setup**
   wizard. The gateway refuses to bind to a non-loopback address with auth
   disabled.
2. The panel issues a signed, `HttpOnly`, `SameSite=Lax` session cookie with a
   12-hour lifetime. Log out to invalidate it.

### Transport security

- **DICOM-TLS** destinations default to `verify_peer=true`; set `cacert` to a
  custom CA bundle when your PACS uses a private CA.
- **S3** destinations default to HTTPS (`use_https=true`).
- **DICOMweb** report/forward transports use HTTPS with configurable TLS
  verification.
- The web admin panel sets security headers on every response: CSP, HSTS,
  `X-Content-Type-Options`, `X-Frame-Options`, and enforces an origin check
  for state-changing API calls (CSRF protection).

### Secrets handling

- Destination credentials are stored encrypted (AES-256-GCM) in the
  `credentials` section, backed by the OS keyring where available (Windows
  Credential Manager / SecretService on Linux).
- Secrets are **never** returned by the API — the Config and diagnostics
  export redact them (`***`).
- The spool database and audit log are encrypted at rest (operating-system
  level encryption; the gateway requires its encryption key before serving).

### DICOM access control

- `receiver.allowed_ae_titles` restricts which AE titles may push studies.
  **Leave it empty only when every sending modality is trusted.** The receiver
  warns at startup if it is accepting associations from any AE title.
- Hub reporting (`audit.hub_reporting`) streams audit events to the hub
  bookkeeper using a Bearer API key. It is **off by default**.

## Retention

The gateway never auto-deletes undelivered or FAILED studies (a copy is always
retained).

- `storage.retention_delivered_days` — delivered studies are purged after this
  window (default 3 days).
- `storage.max_spool_gb` — spool capacity budget (default 20 GB).
- `storage.disk_full_warning_pct` — capacity warning threshold (default 90%).
- `storage.purge_on_disk_full` — when enabled, oldest **delivered** studies are
  purged on capacity breach; undelivered/FAILED studies are never touched.
- `audit.retention_days` — audit log retention (default 365 days). Older
  events are pruned and the chain re-anchored so verification still passes.

## Audit log

The audit log is an append-only, chained SHA-256 hash chain. Each event's hash
is computed over the previous event, so tampering is detectable:

- **Audit → Verify** recomputes the whole chain and reports any broken link.
- **Audit → Export** downloads the events with their chain hashes and the
  current head hash for offline anchoring.
- **Diagnostics export** bundles the redacted config, PHI-scoped audit events,
  and spool summary for support.

## Hub reporting (v1.1)

When `audit.hub_reporting.enabled` is set, the gateway:

1. Registers itself with the hub bookkeeper (`POST /register-gateway`) on first
   run.
2. Streams lifecycle events to `POST /events` with batching and backoff.

**Reporting failure never blocks forwarding.** If the hub is down, the event
queue backs off and events are retried; the receive/forward pipeline is
unaffected. Hub status is visible on the web Dashboard.

## Troubleshooting

| Problem | Likely cause / fix |
|---------|--------------------|
| Modality rejected | Check `allowed_ae_titles`; add the modality's AE title. |
| Studies stuck queued | Forwarder stopped? Destination disabled? Check `forwarding.concurrency`. |
| Repeated "Error" then "Failed" | Destination unreachable or credentials invalid; fix and **Retry**. |
| Disk filling up | Raise `max_spool_gb`, lower `retention_delivered_days`, or enable `purge_on_disk_full`. |
| Audit verify reports breaks | Someone modified the database — restore from backup; do **not** hand-edit rows. |
| Web panel slow with many studies | Queue view is paginated (10k rows render within budget); reduce page size. |
| Updates not appearing | Auto-update checks the signed manifest (see below); ensure outbound HTTPS to the update URL. |

## Updates (v1.1)

The gateway checks a signed update manifest at a static endpoint. Every update
archive carries an Ed25519 signature; an unsigned or invalid signature is
rejected. Updates are applied after user consent and take effect on restart.
Operators can disable update checks via configuration for version-pinned
environments (see ADR-0006).

## Support / diagnostics

Use **Config → Diagnostics** (or `GET /api/diagnostics/export`) to generate a
one-click support bundle: redacted configuration, PHI-scoped audit events,
spool summary, and version. Send this file with any support request — it
contains **no** credentials.
