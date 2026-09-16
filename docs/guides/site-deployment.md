# mercure Gateway — Site Deployment Runbook

End-to-end procedure for deploying a gateway at a clinical site, from network
pre-flight to a signed-off soak. Written for the **desktop** (Tauri tray) and
**headless** (systemd) forms; the USB-dongle variant is
[usb-quickstart.md](usb-quickstart.md) and is best sequenced *after* a
site proves the desktop/headless path first.

Companion docs: user guide (operator walkthrough), admin guide (configuration,
monitoring, security), [backup-restore.md](backup-restore.md) (the D2 runbook),
`systemd/README.md` (headless install), `docs/dev/release-runbook.md` (artifact
provenance).

---

## 1. Pre-flight survey (before touching the site)

Fill this in with the site's IT; every "unknown" is a schedule risk.

| Item | Value | Notes |
|------|-------|-------|
| Appliance name / host | ______ | `general.appliance_name`; shows in the hub + audit. |
| Modality AE titles to accept | ______ | `receiver.allowed_ae_titles`. **List them** — an empty list accepts any AE title (the receiver warns at startup). |
| Gateway DICOM port | 11112 (default) | `receiver.port`. Confirm it's free on the host; modalities point here. |
| Web panel host/port | 127.0.0.1:8080 (default) | Loopback unless the site opts into network admin (§5). |
| Destination(s) | hub / vendor PACS / both | Host, port, AE title for DICOM; URL for DICOMweb; bucket/creds for S3; etc. |
| Destination reachability | ______ | C-ECHO must succeed from the gateway host **before** go-live (§4). |
| Outbound HTTPS for auto-update | ______ | **Clinical VLANs often block it.** The updater fetches a signed manifest from the GitHub release endpoint (ADR-0006). If blocked: either open egress to that host, or set `update.enabled=false` and pin the version (documented in admin guide §Updates). Confirm which. |
| PHI egress to hub | ______ | `audit.phi_scope` (default `minimal`). Agree the scoping with the site before enabling hub reporting. |
| Backup destination | ______ | Where D2 backups land; encrypted, off-box (§6). |
| Master password custody | ______ | Who holds it, in which password manager (ADR-0004). **Lose it and an encrypted config/DB is unrecoverable.** |
| Tauri updater key (release side) | custodial | Not site-facing, but confirm the artifact was signed with the org key (release-runbook §0.1) before install. |

## 2. Provision the host

- **Desktop (Windows/Linux):** install the signed artifact from the GitHub
  release (verify the `.sig`/manifest per release-runbook §3). The tray app
  boots the sidecar and serves the panel on loopback.
- **Headless (Linux):** follow `systemd/README.md` — a supervised user unit,
  secrets via `gateway.env` (`chmod 600`), `enable-linger` so it survives
  logout, boot health-check. Point `MERCURE_GATEWAY_CONFIG` at the site config.
- Either way, confirm the two ports from §1 are the actual listeners
  (`ss -ltnp | grep -E '11112|8080'`).

## 3. Configure (wizard or imported config)

Two paths — pick one, don't mix:

1. **Guided wizard** (`http://localhost:8080`, Setup): identity → receiver →
   destination(s) → test → done. **Time it** — this is the K4 evidence ("≤ 10
   min to first forwarded study"); record the actual minutes in §7. The wizard
   is the intended first-run path for a single site.
2. **Imported golden config:** for fleets, author one `mercure-gateway.json`,
   `POST /api/config/import` (or the Config page). Note: the export path
   redacts destination secrets, so secrets must come from `gateway.env` /
   the keyring (secrets-and-env-overrides.md), not the imported JSON. Set
   `config_version` correctly so migration checks pass — **it is a string**
   (`"1"`, quoted): an unquoted `1` fails schema validation and, on the
   frozen sidecar, aborts boot outright (E1 dry run, 2026-09-16).

Set web auth on if the panel will be network-reachable (§5). Enabling auth
means `/api/system/health` answers 401 — swap the systemd health probe per
`systemd/README.md` §4 before the unit reports healthy.

## 4. Validate each destination (C-ECHO)

Before forwarding live studies, prove every destination answers. The web panel
runs a C-ECHO per destination (wizard step / pipeline view health dot); the
same service backs `web/echo.py`. For a headless/CI check:

```bash
# loopback, auth-off default:
curl -fs http://127.0.0.1:8080/api/pipeline | jq '.destinations[] | {name, health}'
```

All destinations must report reachable (`ok`) before go-live. A destination
that fails C-ECHO will still *accept* C-STOREs into the spool (store-before-ack
means the study is safe on the gateway even when the target is down), so this
is a delivery-assurance step, not a data-loss gate.

## 5. Network exposure decision (security boundary)

Default is loopback-only, no auth (single-user). The gateway now **refuses to
boot** a non-loopback bind with auth disabled (ADR-0007 / D3b) — so a
misconfigured `web_ui.host=0.0.0.0` fails loudly, not silently.

If the site needs the panel on the network:

- **Preferred:** SSH tunnel — `ssh -L 8443:127.0.0.1:8080 <host>` →
  `http://localhost:8443`. SSH provides transport + auth; the panel stays
  loopback. Nothing to configure on the gateway.
- **Direct network serve:** enable `web_ui.auth_enabled` (+ a strong password
  hash via the wizard), **and** set `web_ui.tls_cert_file`/`tls_key_file`
  (a matched pair, hospital-PKI PEM) so PHI never crosses cleartext. Both
  together, not one. `MERCURE_GATEWAY_ALLOW_INSECURE_BIND=1` exists only for
  dev rigs / a TLS-terminating proxy — never on a PHI box.

**Verify the refusal at the site** (proves the build is post-D3b): set
`web_ui.host=0.0.0.0` with auth off, start the gateway → it must exit with the
refusal message, not bind. Then revert to loopback or fix per above.

## 6. Hook up ops (monitoring + backup)

- **Monitoring (D1):** point the site's Prometheus at
  `GET /api/system/metrics` (admin guide §Monitoring has the scrape block and
  four alert rules: disk-near-full, receiver-down, backlog-growth,
  scrape-dead). Add the low-frequency audit-verify cron check. A headless box
  with no monitoring is "unobserved between incidents" — treat the scrape as
  a go-live requirement for unmanned sites.
- **Backup (D2):** schedule the stop/copy/restart (or online `.backup`) runbook
  in [backup-restore.md](backup-restore.md) at a frequency matching the site's
  audit-retention obligation. **Run the restore drill on the throwaway VM now**
  — a backup that has never been restored is a hope, not a plan. Store
  off-box, encrypted; keep the master password separately.

## 7. Soak + sign-off

Send a controlled test study (a phantom / known UID — **not** real PHI) through
a modality or `demo/fake_modality.py`, then observe for 24–48 h:

> **Do this *before* the soak, on every new build:** send a *burst* of three
> distinct studies inside one auto-enqueue debounce window
> (`demo/fake_modality.py` three times back-to-back). All three must reach
> SENT. rc1 silently routed only the last study of a burst (E1 dry run,
> `docs/qa/e1-dryrun.md`) — the soak's single-study probe would not have
> caught it.

- [ ] **Burst check:** 3/3 studies SENT (not just the last).
- [ ] Test study reached every destination (state SENT; audit shows the route).
- [ ] `GET /api/queue/stats` — queue drains to zero backlog (steady-state 0 queued).
- [ ] `GET /api/system/disk` — usage sane, no unexpected growth.
- [ ] `GET /api/audit/verify` → `{"valid": true}`; `verify_audit_anchors.py` exit 0.
- [ ] Prometheus: scrape `up`, no firing alerts, receiver_running stays 1.
- [ ] Restart the gateway → recovery scan returns cleanly, nothing stranded.
- [ ] K4 record: wizard→first-forwarded time ______ min (target ≤ 10).
- [ ] Backup: one successful snapshot + one completed restore drill.
- [ ] Auto-update: either confirmed reachable (egress allowed) or `update.enabled=false` documented.

**Rollback plan:** the gateway never auto-deletes undelivered/FAILED studies,
so reverting an install is safe — the spool retains copies; re-forward is
idempotent. To pull a bad build: stop the service, restore the prior artifact,
`systemctl --user start` — the recovery scan reconciles anything in flight.

Site sign-off + these soak numbers append to the release record (GA evidence,
release-runbook). Any §7 failure → open a finding; do not deploy a second site
until the first is green.

---

## Appendix: per-destination specifics

The forwarding handlers differ in what "reachable" and "credentials" mean —
confirm during §4 for the types in use: SFTP needs `known_hosts` seeded (an
unset value rejects every connection, review H4); DICOM-TLS needs `cacert` for
private-CA PACS; XNAT/S3/DICOMweb need their auth tokens from the keyring/env,
not the config JSON. See the admin guide §Configuration and PRD §5.
