# External security review — booking outreach

Sends `docs/qa/security-review-package.md` (assembled 2026-09-14). Two versions:
email for the formal ask, DM for a warm intro. Fill the `[bracketed]` fields.

## A. Email

**Subject:** External security review — QuantumRAD DICOM gateway (pre-GA, release-candidate stage)

Hi `[Name]`,

We'd like to engage `[firm]` for an external security review of the QuantumRAD
DICOM gateway — a store-and-forward medical-imaging gateway that routes DICOM
studies between hospital modalities and PACS destinations, and handles PHI
throughout. We're at release-candidate stage (`v1.1.0-rc2` on the tag) and want
findings to land **before** the GA cut, not after.

**What we're asking you to look at** (full scope in the attached package):

- The **update channel**: Ed25519 signature enforcement against GitHub releases,
  the signing-key custody model, and downgrade/replay paths. This is our highest-
  consequence surface — a broken trust root is a remote-code-execution path on
  clinical boxes.
- The **web admin panel** transport and auth boundary: loopback-by-default, an
  enforced refusal to bind beyond loopback with auth disabled, optional
  operator-supplied TLS. We want validation that the ladder is defensible for
  shared-machine deployments.
- **PHI-handling correctness** across the receive → spool → forward chain,
  including fsync-before-ack crash safety and a chained-SHA256 audit log with
  external + hub-signed head anchors.
- **Credential storage** and the master-password vault model, including the
  single-point-of-failure in key custody we've documented ourselves.
- Anything in the DICOM / SFTP / S3 / DICOMweb / XNAT destination surface that
  our tests miss.

**What you get:** the private repo pinned at a tag; a threat model; our security
test suites (all runnable, ~3 min); dependency-audit CI records; the ADR set; a
`test-rig` with Orthanc + a hub stub so you can drive synthetic studies
end-to-end without touching live data — there is **no live PHI anywhere** in the
review environment.

**Declared gaps, up front** — so your time goes to discovery, not rediscovery:
the spool database is not SQLCipher-encrypted (HMAC verifier + OS permissions +
site disk encryption instead); the panel uses a single shared password by design
for the loopback single-operator model; there is no built-in log forwarder; TLS
certificates are operator-supplied; SFTP `known_hosts` seeding is manual. We want
your judgment on whether the assumptions behind each are sound, especially the
loopback-auth one.

**Logistics:** we'd book 2–3 weeks, with a short scoping call first. Deliverable
is a written, severity-rated report plus a re-test pass on high/critical fixes.
We're holding a triage buffer between report delivery and our GA tag so findings
can actually be fixed in an `-rcN` rather than deferred.

Would a 30-minute scoping call in the next two weeks work? Happy to send the
package ahead of it.

Best,
`[name]` · `[role]` · `[contact]`

## B. DM (warm intro)

Hi `[Name]` — we're at release-candidate on a DICOM gateway that handles PHI
(store-and-forward, hospital modality → PACS routing) and want an external
security review **before** GA, not after. Highest-priority surfaces are the
Ed25519 updater trust root and the web-admin boundary. We have a full package
(scope, evidence, declared gaps, a test-rig with synthetic data, no live PHI) and
2–3 weeks of calendar. Up for a 30-min scoping call? I'll send the package first.

## C. Booking checklist for us (closes rc-checklist §5 row 4)

- [ ] Scoping call held; scope and deliverable confirmed in writing.
- [ ] Package sent: this outreach's §A + `docs/qa/security-review-package.md` +
      PRD §6 + ADR-0004/0006/0007 + `docs/guides/` set + `rc1-checklist.md`.
- [ ] Repo access granted, **read only, pinned to the review-start tag**.
- [ ] Exchange PGP keys for any sensitive material; findings via encrypted channel.
- [ ] **Findings-triage buffer booked** between expected report date and the
      `v1.1.0` GA tag (plan risk 5) — triage is accept-and-document vs rc-fix on
      `main` with gates re-verified; critical/high get a re-test pass.
- [ ] Record the booking date + reviewer in `rc1-checklist.md` §5 row 4, flip it
      to booked.

## Where a reviewer will probe hardest (have answers ready)

- Updater trust root: key custody SPOF (runbook §0.1), what happens on key
  compromise, downgrade protection. **Note (incident to disclose):** a PAT and the
  signer passphrase were exposed in an agent transcript on 2026-09-14; the PAT had
  scopes sufficient to read the encrypted private-key secret, so the keypair was
  treated as potentially compromised. **After assessment, the maintainer declined
  rotation (2026-09-17)** — the exposed key remains the live signing key, and the
  published `v1.1.0-rc2` artifacts are signed with it. Rotation has no in-band
  path (runbook §0.1: updaters signed with the old key stop verifying), so
  declining the rotation means living with the exposure rather than forcing a
  full re-install at every deployed site. Expect to be asked about both halves —
  why the key was not rotated, and how we'd handle an equivalent exposure across a
  deployed fleet. The fleet answer is drafted below — with its own declared gaps.
- The loopback single-password model vs. a real multi-operator site.
- Spool DB at-rest story vs. the (encrypted) config vault sitting next to it.
- Whether the audit chain's trust assumptions hold when the hub anchor is absent.

### Our fleet-wide key-compromise answer (drafted, with its gaps declared)

Stated as we'd answer it, not as we'd like it to be. The mechanism
(`src/mercure_gateway/update.py`, ADR-0006, config `UpdateConfig`):

**What the compromise of the signing key alone cannot do.** Applying an update
takes more than a valid signature. The updater never auto-installs — it *checks*
at startup and applies only after local operator consent and a restart
(`UpdateConfig.enabled` defaults to `false`; a check is not an install). The
manifest is fetched from GitHub Releases over HTTPS, so a stolen key does not by
itself grant publish rights: an attacker needs the key **and** release-publishing
credentials. Verification is fail-closed in two independent places — Tauri's
updater plugin against the compiled-in key, and the Python-side `Updater` against
`config.update.public_key` (empty key = every archive rejected, never accepted).
A signature that does not verify is a rejection, not a warning.

**What it can do, and what we'd do about it.** A holder of the key plus publish
rights can ship a malicious archive that *will* verify, and if a site accepts the
prompt, it runs. Our response is the runbook §0.1 rotation path, and it is
out-of-band by necessity — the updater trusts only the compiled-in pubkey, so a
rotated key cannot be pushed to an installed fleet: sites must **re-install** a
superseding installer, not update to it. In the window between detection and
re-install, a site can: disable update checks (`config.update.enabled = false`),
pin `update_url` to an internal mirror we control, or pin a site-owned key in
`config.update.public_key` behind a re-signing proxy (correct but operationally
heavy — we would not ask a small site to do it).

**The gaps we are raising ourselves, because a reviewer will find them anyway:**

1. **No in-band revocation.** There is no revocation channel a deployed gateway
   consults; "stop trusting this key" requires an out-of-band installer. This is
   the weakest part of the fleet story and we know it.
2. **The version check is not monotonic.** `Updater.check_update` treats *any*
   version differing from the running one as available — a lower version is
   offered as an update. With a valid signature on an old, vulnerable archive,
   that is a downgrade path even without key compromise, needing only a
   compromised *endpoint*. We consider this a real finding; it is filed for a
   fix on `main` (monotonic / minimum-version comparison) rather than deferred.
3. **Single-key trust root.** The compiled-in key is one secret with no
   continuity plan — no second key waiting in the firmware for exactly this
   case. Multi-key support is a design change we'd want reviewer input on before
   committing to.
4. **Consent is a control, not a barrier.** A verifying-but-malicious update
   still presents as a normal prompt; operator vigilance is load-bearing and
   cannot be assumed at every site.

We would rather have these on the record before the engagement than spend review
hours having them surfaced.
