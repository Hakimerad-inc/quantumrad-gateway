# S09-T7: Release Candidate 1 Checklist (v1.1.0-rc1)

**Date:** 2026-09-14 (partial — Linux-box gates measured; Windows/GH rows open)
**Release captain:** _fill in on tag day_

This checklist derives from PRD §9 (Phase 1/Phase 2 exit criteria — the
"exit checklists in README" that S09-T7 referenced do not exist; §9 and §14
are the actual criteria), §13 Q8 (QA matrix: Win10/11 x64 + Ubuntu), and the
K-gates in §1/refinement §10.

## 1. Automated gates (CI-provable)

| Gate | Target | Measured | ☐ |
|------|--------|----------|---|
| pytest | all green | 668 passed / 4 skipped, 0 failed — clean single run 2026-09-14 (dev box, `901c390`); CI matrix pending | ☒ local |
| coverage | ≥ 80% | **85%** (`coverage report`, src/mercure_gateway, 2026-09-14) | ☒ |
| ruff / mypy strict | clean | ruff: all checks passed · mypy strict: 0 issues / 52 files | ☒ |
| frontend (tsc / eslint / vitest) | clean, all pass | tsc 0 · eslint 0 · vitest **13 tests / 4 files** (jsdom suite replaced by Playwright e2e in `15ade80`) | ☒ |
| e2e (Playwright, real gateway) | green | **25/25 passed locally 2026-09-14** (seeded source-run gateway, ports 18299/18113); fixed a cross-spec order dependency in `pipeline.spec.ts` reports test | ☒ |
| Rust (cargo check / clippy / test) | clean | check ✅ · clippy -D warnings ✅ · test ✅ (0 tests run — Rust unit tests were dropped from the shell; the checklist's earlier "2 unit tests" is stale) — 2026-09-14, Rust 1.98 | ☒ |
| pip-audit | no critical vulns | CI `dependency-audit` job (now also `npm audit --omit=dev` — **0 prod vulns** locally 2026-09-14 — and rustsec) | ☐ CI |
| perf gates (K3/K8, §5.6) | within budget | **forwarding begin 226 ms / 2000 ms; throughput 380.5 items/s / ≥5** (`check_perf_gates.py`, 2026-09-14) | ☒ |
| K6 size (shell + backend bundle) | ≤ 250 MB | _needs package-windows run (Windows blocked — see §6)_ | ☐ |
| deb size | ≤ 500 MB | _fill from local signed deb rehearsal (in progress)_ | 🔄 |
| frozen-backend smoke | health 200 | **Linux ✅ 2026-09-14**: PyInstaller onedir sidecar serves `{"status":"ok","version":"1.1.0-rc1"}`; Windows leg = CI | ☒ local |
| chaos suite (K1/K2) | green | `tests/chaos/` green within the 668 | ☒ |
| security gates | green | `tests/test_security_gates.py`, `tests/test_web_security.py` green within the 668 | ☒ |

## 2. Version & artifacts

- [x] `uv run python scripts/sync_version.py --check` passes (all five sources at `1.1.0-rc1`) — 2026-09-14
- [ ] Release commit tagged `v1.1.0-rc1`; `.github/workflows/release.yml` produces artifacts — **pipeline defects fixed** (`eb3f793`: createUpdaterArtifacts, gh-release-create, merged latest.json, pub_date); execution needs a GitHub remote (see `docs/dev/release-runbook.md` §0.2)
- [ ] Installer + `.sig` sidecars + `latest.json` attached to the GH release — blocked on remote; `.sig` emission being proven locally in the deb rehearsal
- [ ] Updater pubkey (`MERCURE_TAURI_PUBLIC_KEY`) is the real release key, not the dev placeholder — keypair generated locally (custody record in `docs/dev/release-runbook.md` §0.1); secret upload pending

## 3. Clean-VM UAT (the human step — PRD §9 Phase 1 exit criterion)

- [ ] Windows 10 x64: install → wizard → first study forwarded (K4 ≤ 10 min) — `docs/qa/uat-06.md`
- [ ] Windows 11 x64: same walkthrough
- [ ] Ubuntu (supported LTS): deb + AppImage boot and forward — _deb built + sidecar-smoked locally in rehearsal (see §1); full-boot UAT pending_
- [ ] Packaged sidecar spawn verified (tray state transitions idle→sending→error; the one gap CI cannot drive headless)
- [ ] Windows service mode: install/start/stop/uninstall from the admin panel (S07-T9)
- [ ] Auto-update: point a test install at a staging `latest.json`, verify signature enforcement rejects a tampered artifact — local rehearseal recipe: runbook §3 (updater `update_url` is config-overridable)

## 4. K-gate evidence table

| Gate | Criterion | Evidence |
|------|-----------|----------|
| K1 | ≥ 99.9% persisted before ack | chaos suite + `test_store_before_ack` ✅ (in 668) |
| K2 | ≥ 99% delivered within retry budget | chaos suite retry tests ✅ |
| K3 | ≥ 95% reports within SLA | `check_perf_gates.py` 2026-09-14 ✅ (forwarding-begin 226 ms / 2 s budget; SLA row per script's report gate) |
| K4 | ≤ 10 min to first forwarded study | clean-VM UAT timing — Windows hardware |
| K5 | 100% of events audited | `test_audit_coverage.py` ✅ + interop proof `test_hub_bookkeeper_interop.py` (`87d6713`) |
| K6 | ≤ 250 MB installer / ≤ 150 MB RAM | CI size gate (Windows pending); deb number in §1; RAM from UAT |
| K7/K8 | refinement §10 perf criteria | `check_perf_gates.py` ✅ (throughput 380.5/s ≥ 5) |
| K9/K10 | USB boot ≤ 30 s / flush ≤ 10 s | `docs/qa/usb-perf-09.md` — K10 mechanism + proxy timing ✅; boot rows need the rig |

## 5. Known-open items at RC

- S10 boot-mode legs (S01-T8, S09-T9 boot rows, S10-T2/T3/T10/T11) — physical USB hardware
- Hub `/anchor` signing endpoint is documented (`docs/dev/hub-anchor-api.md`) but not implemented hub-side — **gateway side now proven end-to-end against a contract stub** (`test-rig/bookkeeper/`, `tests/test_hub_bookkeeper_interop.py`); live hub interop remains with the hub team (S01-T5/Q7)
- Packaged-sidecar resource-path validation happens only in clean-VM UAT (§3)
- PRD §11 external security review — not yet booked (v1.0 blocker; all T3 prep gates green)

## 6. Blocked-by-environment register (what a Windows-VM + GitHub-enabled box must finish)

| Item | Why blocked here | Where to do it |
|---|---|---|
| Windows installer artifact + K6 size row | no Windows OS here | GH Actions `package-windows` / clean VM |
| GH release + `.sig` + merged `latest.json` execution | repo has no remote yet | runbook §0.2–§2 |
| §3 Windows 10/11 UAT legs, tray/spawn, service mode | Windows hardware | clean VM, `uat-06.md` |
| K9 boot timings + USB flash e2e | physical stick + PC boot matrix | S10 rig, `usb-uat-10.md` |
| loop-device partition e2e (root-only test written) | no root/loop perms (non-root `losetup` EPERM) | any root Linux box — `test_usb_partition.py::test_layout_on_real_loop_device` |
| CI matrix green record (pip-audit/npm/rustsec via Actions) | no Actions without remote | first CI run after push |
