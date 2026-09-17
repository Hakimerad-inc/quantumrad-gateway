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
  scopes sufficient to read the encrypted private-key secret, so the keypair is
  treated as compromised and rotated on 2026-09-17. rc2's published artifacts are
  signed with the exposed key and are superseded by rc3. Expect to be asked how
  we'd handle an equivalent exposure across a deployed fleet — that answer is
  currently weak, and is a finding we'd rather raise ourselves.
- The loopback single-password model vs. a real multi-operator site.
- Spool DB at-rest story vs. the (encrypted) config vault sitting next to it.
- Whether the audit chain's trust assumptions hold when the hub anchor is absent.
