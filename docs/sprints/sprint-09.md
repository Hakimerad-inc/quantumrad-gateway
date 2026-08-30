# Sprint 09 — Hardening, Gates & Release Candidate (Weeks 17–18)

**Goal:** Prove the KPIs and §10 test strategy, ship the remaining v1.1 items (auto-update,
diagnostics export, docs), and cut the v1.1 release candidate.

**Exit criteria:** Failure/chaos tests green (retry + retained copy, disk-full, crash recovery);
perf gates met; dependency audit clean; RC artifacts on Windows + Linux; MVP + Phase 2 exit
checklists ticked.

**PRD refs:** §10 (testing strategy), §12, §5.6, §2.3 v1.1, §6.3.
**Refinement refs:** product-refinement-spec.md §10 (refined success criteria K7/K8).

| ID | Task | PRD ref | RED → GREEN | DoD | Status |
|----|------|---------|-------------|-----|--------|
| S09-T1 | **Chaos suite:** kill destination mid-transfer (retry + retained copy); disk-full simulation; crash-recovery scan rerun | §10, K1, K2 | `tests/chaos/` — each scenario asserts data retention + eventual delivery | K1/K2 mechanism proven under failure | ☐ |
| S09-T2 | **Perf gates in CI:** forwarding latency (≤2 s begin), report SLA (K3), queue-view 10k rows ≤500 ms, idle RAM ≤150 MB; **concurrent forwarding throughput (K8)**; recorded as CI job | §5.6, K3, K6, K8 | gate scripts with budgets; build fails on regression | §5.6 + K8 numbers enforced, not aspirational | ☐ |
| S09-T3 | **Security gates:** pip-audit dependency scanning, TLS config tests, AE allow-list test, audit tamper-evidence re-verified, secrets-not-in-config sweep | §10, §6.3 | `tests/test_security_gates.py` + pip-audit CI job | §10 security row green | ✅ |
| S09-T4 | **Auto-update implementation:** per ADR-0005 — signed update check + apply, user consent flow | §2.3, Q5 | `tests/test_updater.py` — update detection, signature verification (bad signature rejected), apply/rollback | Auto-update v1.1 AC green | ☐ |
| S09-T5 | **Diagnostics bundle export:** one-click support bundle (redacted config + structured logs + spool summary) from web admin panel | §7, §2.3 | `tests/test_diagnostics.py` — bundle contents complete, secrets redacted (reuse S04-T2 redaction) | Support tooling AC green | ☐ |
| S09-T6 | **Docs:** user guide (wizard, daily ops, reports) + admin guide (config, security, retention, troubleshooting) + **USB dongle quick-start guide** | §9 Phase 1/2, usb-dongle-spec | docs build/lint task in CI | §9 docs deliverable done; USB guide ready for Sprint 10 | ☐ |
| S09-T7 | **Release candidate sweep (no TDD):** run both exit checklists in README; tag v1.1.0-rc1; QA matrix per Q8 (Win10/11 x64, Ubuntu) | §9, §13 Q8 | all checklist items ticked | RC tagged; §12 metrics baseline captured | ☐ |
| S09-T8 | **Web admin panel hardening:** XSS prevention, CSRF tokens, input validation on all API endpoints; security headers (CSP, HSTS) | refinement §7 | `tests/test_web_security.py` — XSS payloads rejected; CSRF enforced; security headers present | Web UI security hardened | ☐ |
| S09-T9 | **USB-specific perf baseline (no TDD):** measure boot → gateway ready time on USB (Linux mode + Windows mode); hot-unplug flush time; recovery scan time; record against K9/K10 targets | usb-dongle-spec §10 | perf measurements recorded in `docs/qa/usb-perf-09.md` | K9/K10 baseline established for Sprint 10 | ☐ |

**Evidence:** _(links to commits/PRs when done)_

**Notes:**
- T1 chaos scenarios reuse the S01 rig (Orthanc + fake modality) — no new infrastructure expected.
- T2 budgets come straight from §5.6/KPIs; if a gate can't be met, the failure mode is a filed
  decision (accept + document vs optimize), not a silently skipped gate.
- External security review (§11) is a pre-v1.0-release activity tracked outside the sprint files;
  T3 prepares everything it needs.
- S09-T8 is new — hardens the web admin panel built in Sprint 06 against common web vulnerabilities.
- S09-T9 is new — establishes USB performance baselines before Sprint 10 builds the full USB variant.
- S09-T6 expands the docs scope to include a USB dongle quick-start guide (feeds Sprint 10).
