# Migration Guide — to v1.1.0 GA from rc1 / rc2 / rc3

What changes for a deployed appliance on upgrade, and what to do if something
goes wrong. Read alongside `CHANGELOG.md` (what changed) and
`docs/dev/release-runbook.md` (how a release is published and rolled back).

**Nothing here requires action before upgrading.** Every change below is
either automatic on first boot or is a stricter check that names itself when
it trips. The two that can surprise an operator are flagged.

## Upgrade procedure

The gateway has no in-place data migration step — the upgrade *is* the
installer:

1. Install the new signed installer for the platform (or let the desktop
   updater apply it after consent).
2. Restart. The service/desktop shell restarts; the spool, config, audit
   chain, and credential store are untouched (see `backup-restore.md` for
   their locations).
3. Confirm the reported version in the web panel footer or
   `GET /api/system/status`.

Verify the download before installing — the release body lists every
artifact's sha256, and `sha256sum -c SHA256SUMS` in the download directory
checks all of them.

## Behaviour changes an operator will notice

### Config schema is enforced (was: silently ignored)

Unknown keys are now rejected on write and pruned on boot.

- **On boot:** if the on-disk `mercure-gateway.json` contains keys the schema
  does not know, they are pruned from the loaded config, named in the startup
  log, and the original file is preserved verbatim as
  `mercure-gateway.json.unknown-keys.bak`. The appliance boots. Nothing is
  lost.
- **On save (`PUT /api/config`, `POST /api/config/import`):** an unknown key is
  a **400 naming the path** — e.g. `general.ae_title` if a key landed in the
  wrong section. This is the signal that catches a stale SPA or a hand-edited
  import; fix the key and save again.
- **No `config_version` bump,** because no field changed meaning — the change
  only tightens what is accepted.

The most likely thing to trip this: a config edited by hand with a typo, or a
field that moved between sections. The 400 tells you which.

### Secrets are encrypted at rest by default (was: cleartext unless a master
password was set)

Historically, destination credentials and the admin password hash were written
in cleartext unless `MERCURE_MASTER_PASSWORD` was set in the environment —
which, on a default install, it never was.

- **On first boot after upgrade,** if no master password is resolvable, one is
  generated and persisted via the OS keyring (Windows Credential Manager /
  SecretService on Linux), and the fact is logged. Existing secrets are then
  stored encrypted.
- **Fallback when no keyring is available** (headless box without DBus): the
  generated password is written to a `0600` file beside the config, path
  logged. Worse than a keyring, still not cleartext.
- **Explicit opt-out** for dev rigs and test fixtures:
  `MERCURE_GATEWAY_ALLOW_PLAINTEXT_SECRETS=1`, which logs a startup warning
  and trips the config lint check. Do not set this on a production appliance.

Nothing is lost either way: the cleartext secrets that were there are still
readable to the gateway, they are simply stored encrypted on the next write.

### Admin authentication

- Password hashes are **PBKDF2** (`pbkdf2$<iters>$<salt hex>$<dk hex>`). An
  existing `sha256$salt$hex` hash from an earlier release still verifies — it
  is the only format that exists in the wild — and can be replaced by setting
  a new password.
- **Setting a password** (the thing the old docs got wrong — the "set a hash
  via the setup wizard" path never existed):
  1. `mercure-gateway --set-web-password --config /path/mercure-gateway.json`
     — the CLI rescue path; needs no API session, and is the locked-out
     operator's only recovery.
  2. `POST /api/web-ui/password` — the current password is required when
     authentication is already enabled, so a hijacked stale session cannot
     silently rotate. Rate-limited like login.
  3. The setup wizard's "Admin Password" step posts plaintext; the SPA never
     holds or round-trips a hash.
- **Binding to a non-loopback address with authentication disabled is now a
  startup failure** (was: a warning). If you run behind a TLS-terminating
  reverse proxy or on a dev rig, `MERCURE_GATEWAY_ALLOW_INSECURE_BIND=1`
  downgrades it to a warning. Do not set it on a box that serves PHI over the
  network.
- The session secret is now per-process, so a config save no longer
  invalidates every live session.

### Destination renaming

Renaming a destination while the payload carries a `***` redaction sentinel
used to risk restoring *another* destination's stored secret (the positional
fallback assumed payload order matched stored order). It is gone: if the
payload has no name match, the API returns a **400 naming the destination** —
re-enter the credential rather than have a wrong one persisted. Duplicate
destination names are also now rejected at save.

### Report retrieval (US-06) now works

If you configured report retrieval and it never retrieved anything, this is
why: the C-FIND/C-MOVE transports were complete but unwired, and every poll
failed into a warning. Existing configuration needs no change; check the
Reports view after upgrade.

### Observability

If you scrape `/api/system/metrics`, the new series are:

`mercure_gateway_hub_outbox_depth`, `mercure_gateway_hub_delivering`,
`mercure_gateway_hub_delivered_total`,
`mercure_gateway_hub_delivery_failures_total`,
`mercure_gateway_hub_events_evicted_total`,
`mercure_gateway_audit_anchor_ok`, `mercure_gateway_audit_anchor_errors_total`,
`mercure_gateway_audit_chain_ok`, `mercure_gateway_audit_chain_errors_total`,
`mercure_gateway_purge_iterations_total`,
`mercure_gateway_purge_budget_hits_total`.

**Alert on `hub_outbox_depth`, not on `hub_streaming`.** `_hub_streaming` is
worker *liveness*: it stays 1 while the bookkeeper has been unreachable since
boot, because the worker thread is alive and retrying. Depth nonzero and not
draining is the real signal that the hub is down or not keeping up. Suggested
rules are in `docs/guides/admin-guide.md` (Monitoring).

`audit_anchor_ok` is 0 only when stored hub signatures no longer verify —
investigate audit tampering. It is deliberately 1 when anchoring is unsigned
(no `anchor_public_key` configured), which is "not checked", not "checked and
fine". The new `audit_chain_ok` covers that gap: it replays the chained hashes
every 5 minutes on every deployment and is 0 when a link no longer matches its
neighbours. Alert on it — it is the only integrity series a stock install
emits.

## If the upgrade goes wrong

### The appliance will not boot

The most likely cause of a boot failure after this upgrade is a config the
schema rejects for a *real* reason (not an unknown key — those self-heal).
The startup error names the path. Fix the field and restart.

If you need to get back without fixing:

1. Stop the gateway.
2. Reinstall the previous signed installer (release-runbook §6) — the program
   tree only. The spool database, config, audit chain, and credentials are
   outside it and survive.
3. Restart and confirm the version.

### The web panel locks you out

`mercure-gateway --set-web-password --config <path>` sets a new password hash
without an API session. It requires the config file to be on a filesystem you
can write.

### Something looks wrong with the data

Do not hand-edit the spool database (it breaks the audit hash chain; see
`backup-restore.md`). Restore from a backup, or generate a diagnostics bundle
(`Config → Diagnostics`) and attach it to a support request.

## Downgrade / rollback

There is no in-process rollback, and there never was a working one (review
P1-14 — the `Updater.rollback()` method had zero production callers and is
deleted rather than left as reachable-looking dead code). The Tauri updater is
forward-only: a staged archive swaps at the next restart and the running
binary cannot un-swap itself.

The real procedure is in `docs/dev/release-runbook.md` §6 — reinstall the
previous signed installer; data and config survive; prevent re-application by
pointing `config.update.update_url` away from a broken `latest` or by
disabling update checks for a version-pinned deployment.

`systemd Restart=always` does not help in this case: it restarts a *crashing*
binary, which is the wrong tool when the binary is new and wrong rather than
dead.
