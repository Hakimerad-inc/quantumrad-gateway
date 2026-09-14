# mercure Gateway — Backup & Restore

The gateway's admin guide tells you to "restore from backup" when an audit-chain
verify reports breaks. This document defines that backup — what state exists on
disk, how to snapshot it consistently, and how to restore so the tamper-evidence
posture (K5) survives.

Losing any of the audit state below is not just data loss: the chained-hash
audit log plus its external head anchors are what make the record *provable*.
A backup that captures the database but not the anchor files lets a database
rewrite go undetected; one that captures neither silently downgrades the
compliance story. Treat this procedure as part of the deployment, not an
optional extra.

## What state lives where

Everything is keyed off two paths:

- **Spool directory** — `storage.spool_dir` (default
  `~/.local/share/mercure-gateway/spool`).
- **Platform data directory** — `~/.local/share/mercure-gateway` (the *parent*
  of the default spool dir; the head anchors deliberately live here, outside
  the spool, so a spool-only rewrite cannot forge them).

| Artifact | Path | Why it must be in the backup |
|----------|------|------------------------------|
| Spool database | `<spool_dir>/mercure-gateway.db` (+`-wal`/`-shm`) | Study + route state, audit chain, `hub_outbox` (durable undelivered events), reports table. The single most important file. |
| DICOM files | `<spool_dir>/**` (per-study directories) | The retained copies that make "delivered + retained" (K2) true. Missing files → studies show ERROR after recovery scan. |
| Operations log | `<spool_dir>/operations.log` (+ rotated siblings) | Human-readable tail for troubleshooting; not the audit chain, but support asks for it. |
| Audit head anchors | `~/.local/share/mercure-gateway/audit-heads.txt` and `audit-heads-signed.jsonl` | External integrity proofs (review M4 / signed anchors). **Outside** the spool dir — a backup that only tars the spool misses these. |
| Config JSON | `MERCURE_GATEWAY_CONFIG` (site's `mercure-gateway.json`) | Destinations, rules, retention, web settings. May be encrypted at rest. |
| Environment secrets | `~/.config/mercure-gateway/gateway.env` (systemd deployments) | Master password, hub API key, auth hash — injected from env, intentionally *not* in the config JSON. `chmod 600`. |
| Master password | operator custody (keyring / password manager), **never in the backup** | Derives the DB at-rest verifier + decrypts `credentials.entries`. Lose it and an encrypted config/DB is unrecoverable. |

> **Never back up the master password into the same medium as the encrypted
> database it protects** — that defeats ADR-0004. Store it separately (org
> password manager, escrow), per the key-custody discipline in
> `docs/dev/release-runbook.md` §0.1.

## Consistent snapshot (backup)

SQLite in WAL mode means the `-wal` sidecar holds committed-but-not-yet-
checkpointed transactions. Copying only `mercure-gateway.db` mid-write can
capture a database missing its newest commits — including recent audit events,
which would then *fail* verification on restore. Two safe options:

### Option A — Stop, copy, restart (recommended, simplest correct)

```bash
# headless (systemd user unit):
systemctl --user stop mercure-gateway

# desktop/tray: quit the tray app (this flushes + closes the DB cleanly).

BK=/backup/mercure-gateway/$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$BK"

# spool DB + DICOM files + operations log, in one tree:
cp -a "${MERCURE_GATEWAY_SPOOL:?}/." "$BK/spool/"
# external head anchors — DO NOT skip; they live one level up:
cp -a ~/.local/share/mercure-gateway/audit-heads.txt      "$BK/" 2>/dev/null
cp -a ~/.local/share/mercure-gateway/audit-heads-signed.jsonl "$BK/" 2>/dev/null
# config + env (env holds secrets — copy to a protected destination):
cp -a "$MERCURE_GATEWAY_CONFIG" "$BK/mercure-gateway.json"
cp -a ~/.config/mercure-gateway/gateway.env "$BK/" 2>/dev/null

chmod 700 "$BK"   # the backup contains PHI + secret paths

systemctl --user start mercure-gateway   # resume forwarding
```

Because the gateway is stopped, there is no mid-write race; the `-wal`/`-shm`
files are simply present and copy along with the tree (the recovery scan
reconciles on next boot regardless).

### Option B — Online backup (no downtime) via `sqlite3 .backup`

When you cannot stop the gateway, let SQLite produce a consistent snapshot and
skip the WAL sidecars:

```bash
BK=/backup/mercure-gateway/$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$BK"
sqlite3 "$MERCURE_GATEWAY_SPOOL/mercure-gateway.db" ".backup '$BK/mercure-gateway.db'"
cp -a "$MERCURE_GATEWAY_SPOOL" "$BK/spool-files"        # DICOM copies (best-effort)
cp -a ~/.local/share/mercure-gateway/audit-heads*       "$BK/" 2>/dev/null
cp -a "$MERCURE_GATEWAY_CONFIG" "$BK/"; cp -a ~/.config/mercure-gateway/gateway.env "$BK/" 2>/dev/null
```

`.backup` is transactionally consistent. The *file* copy of DICOMs is
best-effort and may race an in-flight receive — acceptable because a half-copied
new study simply re-receives on next modality send; the *database* (the audit
chain + route state) is the part that must be atomic, and `.backup` guarantees
that for it. Note that events acknowledged after the snapshot won't be in this
backup — that's inherent to online backup; take the file copy close to a quiet
window if it matters.

## Restore + drill

Restore is backup in reverse, and the drill is what proves you can trust it.
Run the drill on a throwaway VM **before** you ever need it for real.

```bash
# 1. On the target box, stop the gateway (fresh install → service may be absent).
systemctl --user stop mercure-gateway 2>/dev/null || true

# 2. Recreate the directory layout the config/env expect.
mkdir -p "$MERCURE_GATEWAY_SPOOL" ~/.local/share/mercure-gateway ~/.config/mercure-gateway

# 3. Restore DB + files + anchors + config + env.
cp -a "$BK/mercure-gateway.db" "$MERCURE_GATEWAY_SPOOL/" 2>/dev/null \
  || cp -a "$BK/spool/mercure-gateway.db" "$MERCURE_GATEWAY_SPOOL/"
cp -a "$BK/spool/." "$MERCURE_GATEWAY_SPOOL/"
cp -a "$BK/audit-heads.txt" "$BK/audit-heads-signed.jsonl" ~/.local/share/mercure-gateway/ 2>/dev/null
cp -a "$BK/mercure-gateway.json" "$MERCURE_GATEWAY_CONFIG"
cp -a "$BK/gateway.env" ~/.config/mercure-gateway/ 2>/dev/null && chmod 600 ~/.config/mercure-gateway/gateway.env

# 4. Supply the master password (custody, NOT from the backup) if config/DB is
#    encrypted. Export MERCURE_MASTER_PASSWORD_FILE or use the keyring, then:
systemctl --user start mercure-gateway
```

### Pass criteria — all four must hold

After restart, the restored node is trustworthy only if every check is green:

```bash
# a) Health: serving, correct version.
curl -fs "http://127.0.0.1:${MERCURE_GATEWAY_PORT:-8080}/api/system/health"

# b) Audit chain intact (the K5 tamper-evidence proof):
curl -fs "http://127.0.0.1:${MERCURE_GATEWAY_PORT:-8080}/api/audit/verify" | jq -e '.valid == true'

# c) External anchors still verify against the chain (signed anchors only, if
#    hub reporting is enabled — the public key comes from the hub team):
uv run python scripts/verify_audit_anchors.py \
  --anchors ~/.local/share/mercure-gateway/audit-heads-signed.jsonl \
  --public-key hub-anchor.pub                  # exit 0

# d) Spool resumed pending work: undelivered studies are re-queued, not lost.
curl -fs "http://127.0.0.1:${MERCURE_GATEWAY_PORT:-8080}/api/queue/stats" \
  | jq '.queued, .sending'   # pending items present if the backup had any
```

If (b) or (c) fail, **stop and investigate — do not resume sending to the
hub/PACS** from a node whose audit chain you cannot vouch for. The most common
cause is a missing/mismatched `audit-heads.txt` (restored the DB but not the
anchor file, or the DB was checkpointed after the anchor was written).

### What the recovery scan does for you

On boot the gateway reconciles the spool directory against the database
(`recovery.recover`, the S02-T6 / S10-T8 scan): DB rows whose files vanished are
marked ERROR (never silently dropped), interrupted sends revert to a
re-claimable state, and nothing auto-deletes an undelivered or FAILED study.
So a restore whose *file* copy lagged its *database* copy degrades to a few
re-queueable ERRORs, not silent loss — but you still want the anchor files
present so (c) proves the chain rather than merely surviving.

## Retention of backups

Match the audit retention window (`audit.retention_days`, default 365) or your
site's compliance schedule — a backup younger than the retention floor cannot
restore a period the site is still obligated to evidence. Keep at least one
backup older than the current chain head's timestamp so a long-lived
compromise (chain rewritten over months) is still detectable against an
independent older anchor set. Store off-box and encrypted; it contains PHI and
secret paths.

## Related

- Admin guide §Audit log / §Retention — the chain and retention semantics.
- `docs/dev/hub-anchor-api.md` — how `audit-heads-signed.jsonl` is produced.
- `docs/guides/secrets-and-env-overrides.md` — master-password + env custody.
- `docs/dev/release-runbook.md` §0.1 — the parallel key-custody discipline.
