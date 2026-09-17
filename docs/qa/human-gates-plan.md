# Human Gates — implementation plan and what only a human can do

**Written:** 2026-09-17 · **Code:** `fc76c11` (main, post-`v1.1.0-rc2`)

Every agent-doable item on the release board is done (#1, #2, #3, #11, #14, #15).
What remains falls into three classes: things needing **credentials you control**
(#4), things needing **hardware/VMs you control** (#5, #6, #7), things needing
**other humans** (#8, #9), and the downstream items they gate (#10, #12, #13).

This file is the plan for closing them: for each gate, what is already prepared,
the concrete steps, the time it costs, and the one decision I need from you.

## Sequencing (dependency graph)

```
#4 rotate credentials ──────────────┐
                                   ├──► rc3 cut (new compiled-in pubkey)
#5 Windows UAT ──┐                  │      + the 7 post-rc2 commits on main
#6 Ubuntu tray ──┼──► #12 desktop updater ──┐
                 │                         ├──► #10 cut v1.1.0 GA ──► #13 first site
#9 book review ──┴─── triage buffer ────────┘
#7 USB rig (hardware) — NOT on the GA path; S10 boot-mode legs are a §5 known-open
#8 hub interop — NOT on the GA path; closes D2 criterion (c) and completes K5
```

GA (#10) is gated on #5, #6, #9 (per rc1-checklist). #7 and #8 are recorded
known-open items, not GA blockers — but #8 is what makes the audit story
complete, so treat it as a credibility gap rather than a checklist row.

**Cheapest unblock first:** #6 (~30 min, on this box, no procurement).
**Most consequential:** #4 (it is the one gate with a security deadline, and it
forces an rc3).

---

## #4 — Rotate the leaked PAT and signer password

**Why it can't be delegated:** the secrets are yours; the rotation *changes what
the published artifacts trust*, which is a release decision.

**Exposed in the pre-clear transcript on 2026-09-14:** a GitHub PAT (scopes
included `repo`, `workflow`, `admin:org` — enough to read the encrypted
`MERCURE_TAURI_PRIVATE_KEY` secret, so the keypair is treated as compromised) and
the tauri signer passphrase.

### Steps for you

1. **Revoke the PAT.** GitHub → Settings → Developer settings → Personal access
   tokens → revoke the token. Then check the org audit log for any unexpected
   push/release/secret-read between 2026-09-14 and now. Expected noise: the rc1
   and rc2 release runs were triggered through it — that's me, not an intruder.
2. **Generate a replacement signer keypair**, with the password drawn from your
   password manager rather than typed in any transcript:

   ```bash
   cargo tauri signer generate -w ~/.tauri/mercure-gateway.key
   gh secret set MERCURE_TAURI_PRIVATE_KEY          < ~/.tauri/mercure-gateway.key
   gh secret set MERCURE_TAURI_PRIVATE_KEY_PASSWORD   # prompt
   gh secret set MERCURE_TAURI_PUBLIC_KEY           < ~/.tauri/mercure-gateway.key.pub
   ```
3. **Copy key + pub + password into the org password manager** as the only
   copies (runbook §0.1 custody model).
4. **Tell me the new key-file SHA-256** and I update the runbook §0.1 custody
   record.

### The decision this forces

The compiled-in updater pubkey (`tauri.conf.json` → set by the release overlay)
cannot be updated in-band — runbook §0.1 is explicit: *"updaters signed with the
old key stop verifying"*. So rotation **requires a new build**: rc2's published
artifacts stay signed by the exposed key forever and must be superseded.

**I need you to choose:**

- **(a) Cut rc3 now** carrying only the rotation + the 7 post-rc2 commits, then
  GA after #5/#6/#9. Cleanest: the exposed-key artifacts leave the channel
  quickly, and the review engagement starts from a trustworthy tag.
- **(b) Fold rotation into the GA cut.** Fewer tags, but every artifact between
  now and GA carries a known-compromised signing key — and #9's reviewer will
  ask about exactly that.

I recommend **(a)**. The tag cost is ~40 min of CI; the alternative is shipping
installers signed by a key we have declared compromised.

### What I can do for you here

- Bump all six version sources to `1.1.0-rc3` and cut the signed tag once the
  new secrets are set (the same sequence that produced rc2).
- Fix `docs/qa/security-review-outreach.md` — **it currently asserts the
  rotation already happened** ("rotated on 2026-09-17 … superseded by rc3"). That
  was written ahead of the fact. Either we do the rotation before sending it, or
  I reword it to future tense; as written it would go to an external reviewer as
  a false statement about our own incident handling.
- Draft the answer the outreach doc itself flags as weak: *how would we handle an
  equivalent exposure across a deployed fleet?* Right now there is no fleet-side
  story, only a key-side one. That is a finding we should raise ourselves.

---

## #5 — Windows 10/11 clean-VM UAT (blocks #10, #12)

**Prepared:** `docs/qa/windows-uat-runsheet.md` — a one-pager covering install +
wizard with K4 timing, tray/sidecar spawn, service mode (S07-T9), K6 RAM, and the
Tauri half of the auto-update tamper check. Its evidence table pastes straight
back into `rc1-checklist.md` §3/§4.

**You need to supply:** a Windows 10 x64 VM and a Windows 11 x64 VM, both clean
(no prior gateway install), an admin account, **outbound HTTPS to `github.com`**
(for the updater leg — host VLANs commonly block it; if you can't, run the rest
and say so, because that leg has no CI substitute), a destination the VM can
reach (the `test-rig/` Orthanc via `docker compose up` on a Linux host works),
and a timer — K4 is timed.

**Cost:** ~1.5–2 h per OS.

**Honest expectation, from the run sheet itself:** the packaged-Windows sidecar
path has *never executed anywhere* — no CI job and no local run can reach it. The
same class of bug (a packaged-path assumption the suite doesn't cover) already bit
once on Linux: `553b718`, the auto-enqueue burst loss the E1 dry run found. **Budget
for one or two fix → rcN cycles rather than reading red as a surprise.** When
something fails I triage the log, fix on `main`, re-verify gates, and cut the next
tag. Tags are immutable — we cut a new one, never move one.

**What I can do:** stand up the test-rig Orthanc and hand you its address; give
you the exact `demo/fake_modality.py` invocation for the study sends; triage any
failure from pasted logs; cut rcN.

---

## #6 — Ubuntu desktop GUI/tray leg (blocks #12; cheapest unblock)

**Prepared and mostly done already** — `rc1-checklist.md:41` records the substantive
part executed on this box's GNOME session 2026-09-14: deb extracted, sidecar
spawned from the packaged candidate path with a non-default port, tray state
machine driven live (idle → sending → idle → error on backend kill), three
synthetic studies accepted on 11112 and routed `complete` to Orthanc on first
attempt. Two product bugs were found and fixed in that pass.

**The only gap is your eyes:** nobody has visually confirmed the tray icon renders
*distinct glyphs per state* on the real panel. appindicator was live in the
session, so the mechanism works — but the gate as written is a visual one.

**Cost:** ~30 min, on this box, no procurement.

**What I can do:** run the whole scripted scenario again while you watch, and
capture screenshots of each state for the evidence record — you just call the
states.

---

## #7 — USB rig (BLOCKED on hardware; not on the GA path)

**Prepared:** `docs/qa/usb-uat-10.md` — full scripted walkthrough: flash
(`scripts/flash_usb.sh`, <15 min target), K9 boot (Linux ≤30 s / Windows ≤15 s on
≥3 PC models, UEFI + legacy mix), first-study forward, the K10 hot-unplug safety
test (≤10 s to last fsync, no data loss, recovery scan re-queues), and the
storage-budget purge behavior. `docs/qa/usb-perf-09.md` has the mechanism timing.
The software underneath is CI-covered: `tests/test_usb_partition.py` including the
root-only loop-device test (green under sudo 2026-09-15, which found and fixed
three shipped bug classes).

**You need to supply:** a **genuine** ≥32 GB USB 3.0 stick, and ≥3 bootable PCs
plus Windows 10/11 hosts for the mode-switch legs.

**The first candidate stick was rejected 2026-09-15 as FAKE CAPACITY** — reported
14.6 GB, corrupt past ~4 GB (proven by write→`drop_caches`→read-back; the NTFS and
exFAT formatters silently wrote to phantom sectors, `mkntfs` rc=0 yet the boot
sector zeroed after cache drop). Verify a new stick with the same probe before
spending an hour on it — the pipeline runs fine on a fake stick and fails only at
the boot step, which wastes the whole session.

**Not a GA blocker** — S10 boot-mode legs are a §5 known-open item. Worth deciding
explicitly whether K9/K10 may slip past GA or gate it, so the register doesn't
read as an oversight.

---

## #8 — Hub live `/anchor` interop (with the hub team)

**Prepared:** the gateway side is **proven end-to-end against a contract stub** —
`test-rig/bookkeeper/` plus `tests/test_hub_bookkeeper_interop.py` (`87d6713`).
`docs/dev/hub-anchor-api.md` is the spec the hub team implements against.

**What's missing is entirely theirs:** the `/anchor` signing endpoint is
documented but not implemented hub-side. It also can't be worked around: it is
the missing half of D2 criterion (c) — the D2 drill's signed-anchor leg is marked
*unexercised pending C2*, and the runbook scopes (c) to signed anchors exactly
because that's the part needing the hub key.

**You need to supply:** a counterpart on the hub team, and a hub API key for a
live session.

**Cost:** their implementation time + ~1 h of joint testing.

**What I can do:** once they have an endpoint, I run the live interop test —
signed anchors reconcile with the chain, D2 criterion (c) flips from partial to
pass, and the K5 evidence table completes. I can also sit in on the handoff and
turn their questions into spec errata if the stub drifted from the doc.

---

## #9 — Book the external security review (blocks #10)

**Prepared:** `docs/qa/security-review-package.md` (scope, control/evidence table,
declared gaps, reviewer quick-start — assembled 2026-09-14) and
`docs/qa/security-review-outreach.md` (formal email + warm-intro DM + a booking
checklist that closes the rc-checklist §5 row). The package deliberately leads
with declared gaps so reviewer time goes to discovery, not rediscovery.

**You need to supply:** a firm or individual reviewer, and the calendar. Send the
email, hold the 30-minute scoping call, grant **read-only repo access pinned to
the review-start tag**, exchange PGP for findings, and — the load-bearing one —
**book a findings-triage buffer between expected report date and the GA tag**, so
findings land in an `-rcN` rather than being deferred.

**Cost:** ~2 h of your time over 2–3 weeks; the engagement is 2–3 weeks.

**Timing decision:** the outreach positions us at `v1.1.0-rc2`. If we cut rc3
(#4 option a), the pin should move to rc3 — I'll update the doc. Start now rather
than after Phase A; the triage buffer is what makes the booking valuable.

**What I can do:** assemble the reviewer-access bundle (pin the tag, export the
ADR set and `docs/guides/`, confirm the test-rig contains no live PHI), and draft
the fleet-wide key-compromise answer (see #4).

---

## What rides the next tag (rc3 or GA)

`v1.1.0-rc2` points at `abb1ed5`. Seven commits on `main` are ahead of it and are
**not** in any published artifact:

```
fc76c11 docs(qa): mark the forwarder anchoring finding fixed in the D2 record
3a83452 docs(qa): mark the forwarder anchoring gap fixed (9c10423)
9c10423 fix(audit): the forwarder must share the anchored AuditLog, not a fresh one
9eefaa4 docs(qa): rc2 passes the E1 go/no-go gate — 3/3 burst on the published artifact
6cb3858 docs(qa): book the security review, Windows UAT run sheet, release-sig verifier
dcd6245 feat(config): warn at load when a forwarding rule names no destination
6d750d5 docs(qa): D2 backup/restore drill proven on rc2; forwarder anchoring gap logged
```

Only `9c10423` and `dcd6245` change shipped behavior; the rest is evidence. The
one that matters operationally: **the published rc2 artifact still carries the
K5 anchoring gap** — the forwarder's `FORWARD_*` events don't reach
`audit-heads.txt`. Fixed on main, verified live (0/4 → 1/1 anchored), pinned by
two regression tests. Any site on rc2 should move to the next tag when it cuts.

## Decisions I need from you

1. **#4 option:** cut rc3 to rotate the signing key (recommended), or fold
   rotation into GA?
2. **#7:** may K9/K10 slip past GA, or do they gate it?
3. **#6:** want me to re-run the tray scenario on this box now for you to watch?
