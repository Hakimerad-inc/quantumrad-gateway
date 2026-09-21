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

### Forwarding rules

`forwarding_rules` narrows which destinations a study is sent to. A study
matching at least one rule is sent only to the targets the matching rules name
(unioned); a study matching no rule is sent to every enabled destination — the
default route. Two spellings are accepted:

```json
"forwarding_rules": [
  { "rule": "modality:CT",      "targets": ["hub"],   "priority": "normal" },
  { "rule": "StudyDescription=*chest*", "targets": ["archive"], "priority": "high" }
]
```

- `modality:CT` — the short form, matched case-insensitively against the
  study's Modality tag. This is what the destinations panel edits.
- `TagName=value` — the general form for any extracted tag. `value` may use
  `*` as a wildcard prefix/suffix (`StudyDescription=*chest*`); matching is
  case-insensitive.

`priority` (`high` > `normal` > `low`) resolves conflicts: among the rules a
study matches, only those at the best matching priority contribute targets.

Only the Modality tag is known at enqueue time, so a rule on any other tag
cannot match on the live path — it applies when a full tag set is available,
e.g. in a rule preview. A rule that cannot be parsed is ignored (the study is
routed by the remaining rules, or takes the default route) and is flagged as a
warning in the panel at save time; check the warnings banner after editing
rules.

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

All series are numeric with fixed labels — no paths, identifiers, or study
metadata (PHI-free by construction). The endpoint sits on the same router as
the rest of the admin API, so when `web_ui.auth_enabled` is set the scrape job
must send the Bearer session token issued by the Setup wizard; with the
loopback default (auth disabled) it answers anonymously.

Working config ships in the repository — no need to transcribe it from this
page:

- [`monitoring/prometheus.yml`](../../monitoring/prometheus.yml) — the scrape
  job (loopback default, `metrics_path: /api/system/metrics`) with the auth
  note inline. Run Prometheus against it, or fold the `scrape_configs` block
  into an existing config.
- [`monitoring/alerts.yml`](../../monitoring/alerts.yml) — the recommended
  rule group, 12 alerts: scrape liveness, disk, the three component-liveness
  gauges, queue backlog and dead letters, hub delivery, the purge budget, and
  the audit anchor.

Every metric named in the rules is exported by the gateway — the names were
checked against the instrumentation in `src/mercure_gateway/web/routes.py`
(`system_metrics`).

### Exported series

| Series | Kind | Meaning |
|--------|------|---------|
| `mercure_gateway_up` | gauge | Always 1 when the web panel answers. |
| `mercure_gateway_uptime_seconds` | gauge | Seconds since process start. |
| `mercure_gateway_build_info{version="…"}` | info | Version as a label; value always 1. |
| `mercure_gateway_receiver_running` | gauge | 1 while the DICOM SCP accepts associations. |
| `mercure_gateway_forwarder_running` | gauge | 1 while the forwarding workers run. |
| `mercure_gateway_report_retriever_running` | gauge | 1 while the report poll loop is active. |
| `mercure_gateway_hub_registered` | gauge | 1 when registered with the hub bookkeeper. |
| `mercure_gateway_hub_streaming` | gauge | 1 while the audit-event *worker thread* is alive — liveness, not delivery health (below). |
| `mercure_gateway_hub_outbox_depth` | gauge | Audit events queued for hub delivery (queued + in flight). |
| `mercure_gateway_hub_delivering` | gauge | 1 while a batch POST to the bookkeeper is in flight. |
| `mercure_gateway_hub_delivered_total` | counter | Events delivered since process start. |
| `mercure_gateway_hub_delivery_failures_total` | counter | Delivery attempts that failed and were requeued or evicted. |
| `mercure_gateway_hub_events_evicted_total` | counter | Events dropped from the bounded delivery queue. |
| `mercure_gateway_audit_anchor_ok` | gauge | 1 when the last scheduled anchor verification passed (below). |
| `mercure_gateway_audit_anchor_errors_total` | counter | Anchor signature lines failing scheduled verification. |
| `mercure_gateway_queue_depth{state="…"}` | gauge | Studies per spool state; `state` is one of `RECEIVED`, `QUEUED`, `SENDING`, `SENT`, `ERROR`, `FAILED`. |
| `mercure_gateway_disk_usage_percent` | gauge | Spool filesystem usage percent. |
| `mercure_gateway_disk_total_bytes` | gauge | Spool filesystem total bytes. |
| `mercure_gateway_disk_free_bytes` | gauge | Spool filesystem free bytes. |
| `mercure_gateway_disk_over_threshold` | gauge | 1 once usage reaches `storage.disk_full_warning_pct`. |
| `mercure_gateway_purge_iterations_total` | gauge | Delivered studies auto-purged since process start. |
| `mercure_gateway_purge_budget_hits_total` | gauge | Checks that exhausted the purge iteration cap. |

Two presence notes: the hub series are emitted as **zeros** when hub reporting
is off, and the two purge series are emitted only when the disk monitor is
part of the running app — it is omitted when the web app is built without it
(the test suite, `--write-default-config`), never on a deployed gateway. A
series that is absent rather than zero is indistinguishable from a healthy one
in Prometheus, which is why the rules above rely on the zero-emitting series.

**Kinds are as-emitted, not as-named.** Both purge series carry a `_total`
suffix but are emitted as **gauges** — `routes.py` calls `gauge()` for them
without a `kind=` argument, so the scrape output advertises `# TYPE … gauge`.
The `_total` suffix reads as a counter convention and the emission site's own
comment calls them counters, but the live output is what a rule author must
match. `increase()` over a gauge still evaluates correctly, so the alert above
is unaffected.

### Hub delivery health

`_hub_streaming` is worker *liveness*: it stays 1 while the bookkeeper has been
unreachable since boot, because the worker thread is alive and retrying. Alert
on these instead:

- `mercure_gateway_hub_outbox_depth` — audit events queued for delivery
  (queued + in flight). Nonzero and not draining means the hub is not keeping
  up (or is down).
- `mercure_gateway_hub_delivering` — 1 while a batch POST is actually in
  flight; distinguishes "attempting" from "abandoned". It is a transient flag,
  not an alert predicate — it is 0 between batches on a healthy gateway.
- `mercure_gateway_hub_delivered_total` / `_hub_delivery_failures_total` /
  `_hub_events_evicted_total` — counters since process start. (All zero, and
  the depth zero, when hub reporting is off.)

`_hub_registered` is 0 on every site with hub reporting off, so it is not an
alert either — read it as a diagnostic on the Dashboard, alongside the depth
and failure counters.

### Audit anchor integrity

`mercure_gateway_audit_anchor_ok` is 1 when the last scheduled verification of
the hub's signatures over the audit chain heads passed (and 1, deliberately,
when anchoring is unsigned — see below). A 0 means stored signatures no longer
verify: investigate audit tampering. `mercure_gateway_audit_anchor_errors_total`
counts the offending lines.

Audit integrity is checked in two complementary ways:

- **Anchor authenticity** (`mercure_gateway_audit_anchor_ok`) is verified on a
  timer every 5 minutes when `audit.hub_reporting.anchor_public_key` is set.
  Only the hub-held Ed25519 signatures can detect a *whole-chain* rewrite —
  the internal hash replay below recomputes, so an attacker who rewrites the
  database recomputes those too. The timer, never the scrape path, does the
  work: each pass is one local file read plus one verify per line.
- **Chain integrity** (`/api/audit/verify`) replays every row per call — fine
  operator-triggered, a self-DoS at 15 s scrape intervals, so it is
  deliberately **not** on the scrape path. Schedule it as a low-frequency
  external check (e.g. cron + `curl … /api/audit/verify | jq -e .valid`).

## Troubleshooting

| Problem | Likely cause / fix |
|---------|--------------------|
| Modality rejected | Check `allowed_ae_titles`; add the modality's AE title. |
| Studies stuck **RECEIVED** (0 routes) | Instances arrived while no destination was enabled, or auto-enqueue failed. Use the **Enqueue** button in the Queue view (shown only for a RECEIVED study with no routes) once a destination is enabled; the **Retry** button will not help here, it only resets *existing* routes. The API equivalent is `POST /api/studies/{id}/enqueue`. |
| Studies stuck queued | Forwarder stopped? Destination disabled? Check `forwarding.concurrency`. |
| Repeated "Error" then "Failed" | Destination unreachable or credentials invalid; fix and **Retry**. |
| Disk filling up | Raise `max_spool_gb`, lower `retention_delivered_days`, or enable `purge_on_disk_full`. |
| Audit verify reports breaks | Someone modified the database — restore from backup ([Backup & Restore](backup-restore.md)); do **not** hand-edit rows. |
| Web panel slow with many studies | Queue view is paginated (10k rows render within budget); reduce page size. |
| Updates not appearing | Auto-update checks the signed manifest (see below); ensure outbound HTTPS to the update URL. |
| Wrong receiver port / unexpected defaults | The binary resolves its config from `--config <path>` and **does not read `MERCURE_GATEWAY_CONFIG` itself** — that env var only works under the systemd unit, which expands it into `ExecStart`'s `--config`. Run the binary directly without `--config` and it falls back to `./mercure-gateway.json` relative to its CWD and binds defaults *silently*, which can collide with another gateway on the same box. Always pass `--config` explicitly (or launch from a directory holding the intended file) and confirm the receiver port in the startup log. |

The two actions are deliberately distinct: **Retry** re-arms routes that
already exist (a FAILED study gets a fresh delivery attempt); **Enqueue**
creates the routes in the first place (a RECEIVED study that never got any).
Neither deletes the stored instances — undelivered copies are retained
(US-04).

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
