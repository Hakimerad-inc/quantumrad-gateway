# Human Gates — implementation plan and what only a human can do

**Written:** 2026-09-17 · **Code:** `fc76c11` (main, post-`v1.1.0-rc2`)

Every agent-doable item on the release board is done (#1, #2, #3, #11, #14, #15).
Since this plan was written, two gates closed without waiting on a human: **#6
(Ubuntu tray) finished 2026-09-17**, and **#4 (credential rotation) was declined
by the maintainer 2026-09-17** — see its section below. What remains: things
needing **hardware/VMs you control** (#5, #7), things needing **other humans**
(#8, #9), and the downstream items they gate (#10, #12, #13).

This file is the plan for closing them: for each gate, what is already prepared,
the concrete steps, the time it costs, and the one decision I need from you.

## Sequencing (dependency graph)

```
#4 rotate credentials — DECLINED 2026-09-17 (no rc3 is forced by key rotation)
     ┌─ the 10 post-rc2 commits on main still argue for a next tag (§below)
#5 Windows UAT ──┐
#6 Ubuntu tray ──┼──► #12 desktop updater ──┐   (#6 DONE 2026-09-17)
                 │                         ├──► #10 cut v1.1.0 GA ──► #13 first site
#9 book review ──┴─── triage buffer ────────┘
#7 USB rig (hardware) — NOT on the GA path; S10 boot-mode legs are a §5 known-open
#8 hub interop — NOT on the GA path; closes D2 criterion (c) and completes K5
```

GA (#10) is gated on #5, #6, #9 (per rc1-checklist) — and #6 is now done, so the
GA gate is #5 + #9. #7 and #8 are recorded known-open items, not GA blockers —
but #8 is what makes the audit story complete, so treat it as a credibility gap
rather than a checklist row.

**Cheapest unblock remaining:** #9's booking email (~20 min, you send it; the
package is already assembled). The last on-box unblock (#6) is closed.

---

## #4 — Rotate the leaked PAT and signer password — CLOSED: DECLINED 2026-09-17

**Decision (maintainer, 2026-09-17): rotation is not necessary; it will not
happen.** The exposure stands disclosed (below and in
`security-review-outreach.md`), the exposed key remains the live signing key, and
`v1.1.0-rc2` is **not** superseded. This closes the gate: no rc3 is forced by key
rotation.

The residue of declining, stated plainly so it isn't rediscovered later: runbook
§0.1 has no in-band rotation path — the compiled-in updater pubkey cannot be
swapped without a new build, and updaters signed with the old key stop verifying —
so declining means living with the exposed key rather than forcing a full
re-install at every deployed site. A reviewer is still likely to ask about both
halves (why we did not rotate, and the fleet-wide answer), and the fleet answer
remains weak. That is now a declared gap rather than a pending action.

**Why it couldn't be delegated:** the secrets were yours; the rotation *changes
what the published artifacts trust*, which was a release decision — now made.

**Exposed in the pre-clear transcript on 2026-09-14:** a GitHub PAT (scopes
included `repo`, `workflow`, `admin:org` — enough to read the encrypted
`MERCURE_TAURI_PRIVATE_KEY` secret, so the keypair is treated as compromised) and
the tauri signer passphrase.

### If the decision is ever revisited

The rotation procedure is retained for the record, superseded but not deleted:

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

Rotation then **requires a new build** (there is no in-band path), which is what
made it consequential enough to be a decision rather than a chore. The earlier
(a)-cut-rc3-now / (b)-fold-into-GA choice is settled by the decline: neither.

### What is left on this gate

- ~~Bump all six version sources to `1.1.0-rc3` and cut the signed tag once the
  new secrets are set~~ — superseded by the decline (a next tag would carry only
  the post-rc2 fixes; see decision 4).
- ~~Fix `docs/qa/security-review-outreach.md`~~ — **done 2026-09-17**: it no longer
  asserts the rotation happened; the incident note now states the exposure, the
  declined rotation, and that rc2's artifacts remain signed by the exposed key.
- **Draft the answer the outreach doc itself flags as weak:** *how would we handle
  an equivalent exposure across a deployed fleet?* Right now there is no
  fleet-side story, only a key-side one. That is a finding we should raise
  ourselves, and declining rotation makes it more likely to be asked.

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

## #6 — Ubuntu desktop GUI/tray leg — DONE 2026-09-17

**Closed by `docs/qa/b3-tray-leg.md`** (commits `a84d9e6`, `3e76012`): the
visual-glyph gap this plan called "the only thing left" was closed without a
human at the panel — the glyphs were read live from the session's
StatusNotifierItem over D-Bus and pairwise pixel-diffed (idle↔sending 312/1024
px, idle↔error 322, sending↔error 334), so the states are provably distinct
rather than eyeballed. A 120-instance study forwarded to the test-rig Orthanc
and went SENT; audit chain valid throughout.

The substantive half ran 2026-09-14 (rc1) and is recorded at
`rc1-checklist.md:41`: deb extracted, sidecar spawned from the packaged candidate
path with a non-default port, tray state machine driven live, three synthetic
studies routed `complete` on first attempt. Two product bugs were found and fixed
in that pass; a third (the dead-backend login screen, `b0e74d4`) was found in
*this* leg and also fixed.

**The caveat that remains:** the verification was programmatic, not a human eye
on the panel. The evidence composite is `docs/qa/evidence/b3-tray-states.png` if
you want the visual sign-off anyway — it's a one-look confirmation, not a
re-run of the drill.

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

**Timing decision:** the outreach positions us at `v1.1.0-rc2`, and the review-pin
stays there — with #4 declined there is no rc3 pending for rotation reasons. If
you do cut a fixes tag (decision 4 below), move the pin before granting access;
a pinned tag is only meaningful if it's the tag the reviewer actually reads. Start
now rather than after Phase A; the triage buffer is what makes the booking
valuable.

**What I can do:** assemble the reviewer-access bundle (pin the tag, export the
ADR set and `docs/guides/`, confirm the test-rig contains no live PHI), and draft
the fleet-wide key-compromise answer (see #4).

---

## What rides the next tag — rc3 is cut; this is now the GA list

**`v1.1.0-rc3` was cut and published 2026-09-17** (tag at `d7bd58e`, release run
35221436901 green, deb Ed25519-verified against the §0.1 custody key — see
`rc1-checklist.md` §2). It carries the ten post-rc2 commits, so the K5 anchoring
gap, the dead-backend login screen, and the stale-rule warning are now in a
published artifact; any site on rc2 should move to rc3.

Two commits landed on `main` after the tag and ride the GA cut:

```
fb6455f fix(update): a differing version is not an upgrade — gate the downgrade path
4ad00cd docs(qa): draft the fleet-wide key-compromise answer, gaps declared
```

Only `fb6455f` changes shipped behavior — the downgrade gate. Two notes for the
GA cut:

- rc3's published manifest now correctly advertises `1.1.0-rc3` to a box on rc2
  and offers nothing to a box on rc3. Confirm the panel shows that during #5's
  Windows UAT — it is the first time the "no update available" path sees a
  real, newer published release.
- `fb6455f`'s `_version_key` parses only the two forms `sync_version.py`
  publishes (`X.Y.Z`, `X.Y.Z-rcN`) and fails closed on anything else. That is
  deliberate — an unorderable version must not be offered — but it means a
  future scheme change needs a matching parser update, or the updater silently
  stops offering updates.

## Decisions I need from you

1. ~~**#4 option:** cut rc3 to rotate the signing key, or fold rotation into GA?~~
   **Resolved 2026-09-17: rotation declined; no rc3 forced.** (The next tag, if
   cut, carries only the post-rc2 fixes above.)
2. **#7:** may K9/K10 slip past GA, or do they gate it?
3. ~~**#6:** re-run the tray scenario for you to watch?~~ **Resolved 2026-09-17:
   done** — programmatic glyph verification closed it; see `b3-tray-leg.md`. The
   evidence PNG is in the repo if you still want the one-look sign-off.
4. ~~**Cut the next tag to ship the three post-rc2 behavior fixes?**~~
   **Resolved 2026-09-17: cut.** `v1.1.0-rc3` is published and custody-verified;
   rc2's K5 anchoring gap is out of the channel. What remains on the GA path is
   #5 (Windows UAT) and #9 (book the review) — plus deciding #2 above.
