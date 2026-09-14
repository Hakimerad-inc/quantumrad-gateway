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
   disabled (startup fails with an explanatory error; loopback = `127.0.0.1`,
   `localhost`, `::1`). A deliberate exception — dev rigs or a TLS-terminating
   reverse proxy on the network — is `MERCURE_GATEWAY_ALLOW_INSECURE_BIND=1`,
   which downgrades the refusal to a startup warning. Do not set it on
   production boxes that serve PHI.
2. The panel issues a signed, `HttpOnly`, `SameSite=Lax` session cookie with a
   12-hour lifetime. Log out to invalidate it.

### Transport security

- **DICOM-TLS** destinations default to `verify_peer=true`; set `cacert` to a
  custom CA bundle when your PACS uses a private CA.
- **S3** destinations default to HTTPS (`use_https=true`).
- **DICOMweb** report/forward transports use HTTPS with configurable TLS
  verification.
- **Web admin panel** (ADR-0007): binds loopback over plain HTTP by default
  (single-user; the OS user boundary is the auth boundary). For remote
  administration prefer an SSH tunnel — `ssh -L 8443:127.0.0.1:8080
  <gateway-host>` then browse to `http://localhost:8443` — so SSH provides
  transport + authentication. To serve the panel on the network directly, set
  `web_ui.tls_cert_file` + `web_ui.tls_key_file` (a matched pair — hospital
  PKI or any operator-managed PEM; half a pair is a config error) *and* enable
  `web_ui.auth_enabled`.
- Security headers: CSP, `X-Content-Type-Options: nosniff`, and
  `X-Frame-Options: DENY` on every response, plus an origin check for
  state-changing API calls (CSRF). `Strict-Transport-Security` is sent only
  over TLS (browsers ignore it on plain HTTP — see ADR-0007).

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
  bookkeeper using a `Token`-scheme API key (HTTP header
  `Authorization: Token …`, per TD-19). It is **off by default**.

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

## Monitoring (headless deployments)

The gateway exposes a Prometheus text-exposition scrape target:

    GET /api/system/metrics   →  text/plain; version=0.0.4

All series are numeric gauges with fixed labels — no paths, identifiers, or
study metadata (PHI-free by construction). Key series:
`mercure_gateway_up`, `mercure_gateway_uptime_seconds`,
`mercure_gateway_receiver_running` / `_forwarder_running` /
`_report_retriever_running`, `mercure_gateway_hub_registered` /
`_hub_streaming`, `mercure_gateway_queue_depth{state="…"}`,
`mercure_gateway_disk_usage_percent` / `_disk_total_bytes` /
`_disk_free_bytes` / `_disk_over_threshold`.

Scrape config (loopback default; with `web_ui.auth_enabled` the job needs the
session Bearer token from the Setup wizard credentials):

```yaml
scrape_configs:
  - job_name: mercure-gateway
    static_configs:
      - targets: ["127.0.0.1:8080"]
    metrics_path: /api/system/metrics
```

Recommended alerts (Prometheus rule syntax):

```yaml
groups:
  - name: mercure-gateway
    rules:
      - alert: GatewayDiskNearFull
        expr: mercure_gateway_disk_over_threshold == 1
        for: 5m
      - alert: GatewayReceiverDown
        expr: mercure_gateway_receiver_running == 0
        for: 10m
      - alert: GatewayBacklogGrowing
        expr: increase(mercure_gateway_queue_depth{state="QUEUED"}[30m]) > 50
      - alert: GatewayScrapeDead
        expr: up{job="mercure-gateway"} == 0
        for: 5m
```

The audit-chain integrity gauge is deliberately **not** on the scrape path —
`/api/audit/verify` replays the whole chain per call (fine operator-triggered,
a self-DoS at 15 s scrape intervals). Schedule it as a low-frequency external
check (e.g. cron + `curl … /api/audit/verify | jq -e .valid`).

## Troubleshooting

| Problem | Likely cause / fix |
|---------|--------------------|
| Modality rejected | Check `allowed_ae_titles`; add the modality's AE title. |
| Studies stuck queued | Forwarder stopped? Destination disabled? Check `forwarding.concurrency`. |
| Repeated "Error" then "Failed" | Destination unreachable or credentials invalid; fix and **Retry**. |
| Disk filling up | Raise `max_spool_gb`, lower `retention_delivered_days`, or enable `purge_on_disk_full`. |
| Audit verify reports breaks | Someone modified the database — restore from backup ([Backup & Restore](backup-restore.md)); do **not** hand-edit rows. |
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
spool summary, and version. Send this file with any support request.

**PHI posture of support artifacts (PRD §6.4):**

- The diagnostics bundle strips credential material — send it with confidence
  over normal support channels. It is **not** PHI-free by default: with
  `audit.phi_scope="minimal"` (the default) patient identifiers are reduced,
  but study UIDs and accession numbers remain for reproducibility. If a site
  requires fully de-identified bundles, set `phi_scope` accordingly before
  export.
- The operations log honors the same `phi_scope`: `patient_name`, `mrn`, and
  `patient_id` are stripped from structured fields under `minimal`.
- The raw audit database is never in the bundle; only the PHI-scoped event
  export is.

### Logs: locations, rotation, and shipping

Two independent log surfaces — don't confuse them during triage:

| Source | Where | Rotation / retention |
|--------|-------|----------------------|
| Operations log (`TextLog`) | `<spool_dir>/operations.log` (+ `.1`…`.5`) | Self-rotating at 10 MB × 5 backups. Tail it for operator-readable history; browse via the SPA or `GET /api/logs`. |
| Process stdout/stderr | systemd: `journalctl --user -u mercure-gateway` | journald defaults (volatile size-capped). Persist by setting `Storage=persistent` in `journald.conf` if the box must survive reboots. |

Shipping: the gateway has no built-in log forwarder (a deliberate v1 choice —
one fewer network egress on a clinical VLAN). Use the standard host plumbing:

- **journald-native shippers** (e.g. `systemd-journald` → `journal-upload`, or
  vector/fluentbit with a journald input) collect the process log.
- For the operations log, ship `<spool_dir>/operations.log*` with any file
  shipper; the `.1`…`.5` rotation naming is plain numeric suffixes.
- If the target log system is multi-tenant, route gateway logs to a
  PHI-adjacent access tier: the ops log is `minimal`-scoped but still
  clinical-context data.
- Fastest answer for most support tickets: attach a fresh diagnostics bundle
  (`GET /api/diagnostics/export`) rather than raw logs — it already combines
  the redacted config, recent audit events, and spool state.
