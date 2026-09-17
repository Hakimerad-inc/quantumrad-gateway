# D2 Backup/Restore Drill — v1.1.0-rc2

**Date:** 2026-09-17 · **Box:** `dev@linux workstation` · **Code:** `abb1ed5` (v1.1.0-rc2)
**Runnable form:** `bash scripts/d2_backup_restore_drill.sh`

The backup/restore runbook (`docs/guides/backup-restore.md`) defined the
procedure but it had never been executed. This drill runs it end to end —
back up a loaded node, simulate total loss, restore, and check the pass
criteria — against a throwaway instance, so the first time anyone follows
the runbook is not the first time they need it.

## Setup and isolation

The drill instance is fully isolated from the live gateway on this box:

- **Throwaway `HOME`** — the head-anchor path is `$HOME`-keyed
  (`main.py:_audit_anchor_path`), so the drill's anchors never touch the
  live node's `audit-heads.txt`.
- Own spool dir, own config, web on `127.0.0.1:8091`, receiver on `11123`.
- **Deliberately unreachable destination** (`127.0.0.1:42999`). The studies
  must stay undelivered so criterion (d) has pending work to prove it
  survived the round-trip.
- Seeded with two real fixture studies (31 instances) sent via DCMTK
  `storescu` as a modality would.

## Results

| Check | Result |
|---|---|
| (a) Health + correct version after restore | ✅ `{"status":"ok","version":"1.1.0-rc2"}` |
| (b) Audit chain verifies | ✅ `{"valid":true,"errors":[]}` |
| (c) External anchors reconcile with the chain | ⚠️ partial — see below |
| (d) Undelivered work retained, re-queued | ✅ 2 studies / 31 files present; recovery scan re-queues |

Both documented backup paths round-trip:

- **Option A** (stop, copy tree incl. `-wal`, restart) — restored node passes
  (a), (b), (d). The WAL sidecar mattered: the live DB was 4 KB with a
  2.9 MB uncheckpointed WAL, so a main-file-only copy would have silently
  dropped the newest commits.
- **Option B** (online consistent snapshot) — also round-trips. The `sqlite3`
  CLI is absent on this box; `VACUUM INTO` is the equivalent
  transactionally-consistent snapshot (integrity_check `ok`, row counts
  identical to live). The snapshot is self-contained — a stale 0-byte `-wal`
  left beside it by an earlier failed `Connection.backup()` attempt was
  removed before restore, since a sidecar can shadow a self-contained
  snapshot on open.

### Criterion (c) — honestly partial

The runbook scopes (c) to "signed anchors only, if hub reporting is
enabled". Hub reporting is off in the drill config, and the hub signing
endpoint is not implemented hub-side (C2, with the hub team), so the
signed-anchor leg could not be exercised. What *was* proven locally: every
head in the restored `audit-heads.txt` traces to a real `audit_events.hash`
link — no forged or orphaned anchors.

### The controls that make the pass criteria meaningful

- **Append-only trigger.** A direct `UPDATE audit_events SET hash=...` is
  rejected by the SQL layer (`IntegrityError: audit_events is append-only`
  — `BEFORE UPDATE`/`BEFORE DELETE` triggers). In-place row rewriting is
  structurally prevented.
- **Control 2 (anchor omission).** Restoring the database *without* the
  anchor file yields a node that reports `healthy`, `audit: valid`, correct
  queue stats, and **zero log lines about the missing anchor**. The chain
  verifies without its external proof. So criterion (c) is the *only*
  sentinel for anchor absence — an operator who skips the anchor file in a
  restore gets no warning anywhere. This is why the finding below matters.

## Finding: forwarding events never reach the external anchor

**Severity: weakens K5 tamper-evidence for delivery provenance. Should be
triaged before GA (E2 gate) and handed to the external review (D5).**

Mapping each anchor line back to its event id gave a clean signal:

| Event type | Anchored |
|---|---|
| `STUDY_QUEUED` | 2/2 |
| `PRUNE_AUDIT` | 2/2 |
| `FORWARD_START` | **0/4** |
| `FORWARD_ERROR` | not emitted in this run (destination never connects) |

(The earlier manual run, with a longer forwarding phase, showed the same
shape: `FORWARD_START` 0/3, `FORWARD_ERROR` 0/2, all other types fully
anchored. The gap is the forwarder's, not this workload's.)

Cause: `main.py:169` constructs the forwarder with a **fresh
`AuditLog(database)`**, separate from the `audit` object that
`_wire_head_anchorer` equips with the head anchorer (`main.py:464` /
`_wire_head_anchorer` → `set_head_anchorer`). The forwarder's instance has
no anchorer, so its events are never appended to `audit-heads.txt`. The
same wiring gap means the forwarder's events also miss the hub event
stream — `_start_hub_reporting` attaches its sink via `set_sink` on the
main `audit` only.

The intent documented in the code is that "every chain head is appended to
an external file" (`main.py` comment at the wiring site). In practice, on a
node whose only recent activity is forwarding — the normal steady state —
the unanchored tail is exactly the delivery-provenance record: which study
went where, and what failed.

Mitigating context (why this is "weakens" rather than "breaks"): the chain
is sequential, so forging an event generally perturbs later links and
breaks anchors written after it; and the append-only trigger blocks in-place
rewrites, so a rewrite must go through the trigger-drop + recompute-from-
genesis path that `prune()` already uses. Detection then rests on comparing
the current chain head against an **older independent copy** of the anchors
— which is precisely the forensic value of these backups, and why the
runbook's retention advice (keep at least one backup older than the current
chain head) is load-bearing.

Suggested fix (small, but post-rc2 — it changes wiring, not the tag): build
the forwarder after the audit is wired, or expose an audit setter, so the
forwarder shares the anchored/streaming `AuditLog`.

## Caveats

- The drill config is **unencrypted at rest**; the master-password leg of
  the runbook's restore step 4 was not exercised (no password to supply).
  An encrypted-config restore still needs that custody step.
- Single box, single run. The procedure is now proven once, on Linux; the
  Windows/service-mode restore path remains untested (§3 UAT legs).
- Criterion (c)'s signed-anchor leg is unexercised pending C2.

## Related

- `docs/guides/backup-restore.md` — the runbook this drill validates
- `scripts/d2_backup_restore_drill.sh` — the drill, repeatable
- `docs/qa/rc1-checklist.md` §5 — open-items register
