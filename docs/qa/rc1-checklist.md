# S09-T7: Release Candidate 1 Checklist (v1.1.0-rc1)

**Date:** _fill in_
**Release captain:** _fill in_

This checklist derives from PRD §9 (Phase 1/Phase 2 exit criteria — the
"exit checklists in README" that S09-T7 referenced do not exist; §9 and §14
are the actual criteria), §13 Q8 (QA matrix: Win10/11 x64 + Ubuntu), and the
K-gates in §1/refinement §10.

## 1. Automated gates (CI-provable)

| Gate | Target | Measured | ☐ |
|------|--------|----------|---|
| pytest | all green | 618+ passed / 4 skipped (see measured-facts addendum in `docs/CODE_REVIEW-2026-09-08.md`) | ☐ |
| coverage | ≥ 80% | _fill in from `pytest --cov`_ | ☐ |
| ruff / mypy strict | clean | _fill in file count_ | ☐ |
| frontend (tsc / eslint / vitest) | clean, all pass | 42 tests in 10 files | ☐ |
| Rust (cargo check / clippy / test) | clean | 2 unit tests | ☐ |
| pip-audit | no critical vulns | CI `dependency-audit` job | ☐ |
| perf gates (K3/K8, §5.6) | within budget | CI `perf-gates` job | ☐ |
| K6 size (shell + backend bundle) | ≤ 250 MB | _fill in from package-windows run_ | ☐ |
| deb size | ≤ 500 MB | _fill in from package-linux run_ | ☐ |
| frozen-backend smoke | health 200 | CI smoke steps (both OS) | ☐ |
| chaos suite (K1/K2) | green | `tests/chaos/` | ☐ |
| security gates | green | `tests/test_security_gates.py`, `tests/test_web_security.py` | ☐ |

## 2. Version & artifacts

- [ ] `uv run python scripts/sync_version.py --check` passes (all five sources at `1.1.0-rc1`)
- [ ] Release commit tagged `v1.1.0-rc1`; `.github/workflows/release.yml` produces artifacts
- [ ] Installer + `.sig` sidecars + `latest.json` attached to the GH release
- [ ] Updater pubkey (`MERCURE_TAURI_PUBLIC_KEY`) is the real release key, not the dev placeholder

## 3. Clean-VM UAT (the human step — PRD §9 Phase 1 exit criterion)

- [ ] Windows 10 x64: install → wizard → first study forwarded (K4 ≤ 10 min) — `docs/qa/uat-06.md`
- [ ] Windows 11 x64: same walkthrough
- [ ] Ubuntu (supported LTS): deb + AppImage boot and forward
- [ ] Packaged sidecar spawn verified (tray state transitions idle→sending→error; the one gap CI cannot drive headless)
- [ ] Windows service mode: install/start/stop/uninstall from the admin panel (S07-T9)
- [ ] Auto-update: point a test install at a staging `latest.json`, verify signature enforcement rejects a tampered artifact

## 4. K-gate evidence table

| Gate | Criterion | Evidence |
|------|-----------|----------|
| K1 | ≥ 99.9% persisted before ack | chaos suite + `test_store_before_ack` |
| K2 | ≥ 99% delivered within retry budget | chaos suite retry tests |
| K3 | ≥ 95% reports within SLA | CI perf gates |
| K4 | ≤ 10 min to first forwarded study | clean-VM UAT timing |
| K5 | 100% of events audited | `test_audit_coverage.py` |
| K6 | ≤ 250 MB installer / ≤ 150 MB RAM | CI size gate; RAM from UAT notes |
| K7/K8 | refinement §10 perf criteria | `scripts/check_perf_gates.py` output |
| K9/K10 | USB boot ≤ 30 s / flush ≤ 10 s | `docs/qa/usb-perf-09.md` (S09-T9, open) |

## 5. Known-open items at RC

- S09-T9 / S10-T10 USB perf baselines not yet recorded (Sprint 10 tasks)
- Hub `/anchor` signing endpoint is documented (`docs/dev/hub-anchor-api.md`) but not implemented hub-side — signed anchoring degrades to file-only until then
- Packaged-sidecar resource-path validation happens only in clean-VM UAT (§3)
