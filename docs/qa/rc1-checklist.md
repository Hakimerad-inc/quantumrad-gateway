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
| pytest | all green | 712 passed, 0 failed — CI matrix green on main (run 34901586844, ubuntu + windows) | ☒ CI |
| coverage | ≥ 80% | **85%** (`coverage report`, src/mercure_gateway, 2026-09-14) | ☒ |
| ruff / mypy strict | clean | ruff: all checks passed · mypy strict: 0 issues / 52 files | ☒ |
| frontend (tsc / eslint / vitest) | clean, all pass | tsc 0 · eslint 0 · vitest **14 tests / 4 files** (jsdom suite replaced by Playwright e2e in `15ade80`) | ☒ |
| e2e (Playwright, real gateway) | green | **25/25 passed locally 2026-09-14** (seeded source-run gateway, ports 18299/18113); fixed a cross-spec order dependency in `pipeline.spec.ts` reports test | ☒ |
| Rust (cargo check / clippy / test) | clean | check ✅ · clippy -D warnings ✅ · test ✅ **4 unit tests** (`backend_port` override/fallback + candidate ordering + deb-layout regression — re-added with the tray fixes, 2026-09-14); earlier "0 tests" row was stale | ☒ |
| pip-audit | no critical vulns | CI `dependency-audit` job **green on main** (run 34901586844, 2026-09-14): pip-audit + `npm audit --omit=dev` + rustsec (`cargo audit`, src-tauri) | ☒ CI |
| perf gates (K3/K8, §5.6) | within budget | **forwarding begin 226 ms / 2000 ms; throughput 380.5 items/s / ≥5** (`check_perf_gates.py`, 2026-09-14); CI `performance gates` job green | ☒ |
| K6 size (shell + backend bundle) | ≤ 250 MB | **CI `package-windows` job: `check_installer_size.py` exe + onedir bundle under 250 MB** (run 34901586844) | ☒ CI |
| deb size | ≤ 500 MB | **69.6 MB** locally signed build 2026-09-14 (`scripts/rehearse_signed_build.sh`); CI `package-linux` gate green | ☒ |
| frozen-backend smoke | health 200 | **Linux ✅ 2026-09-14**: PyInstaller onedir sidecar serves `{"status":"ok","version":"1.1.0-rc1"}`; **Windows ✅ CI**: `package-windows` pwsh health check green (run 34901586844) | ☒ |
| chaos suite (K1/K2) | green | `tests/chaos/` green within the 668 | ☒ |
| security gates | green | `tests/test_security_gates.py`, `tests/test_web_security.py` green within the 668 | ☒ |

## 2. Version & artifacts

- [x] `uv run python scripts/sync_version.py --check` passes (all five sources at `1.1.0-rc1`) — 2026-09-14
- [x] Release commit tagged `v1.1.0-rc1`; `.github/workflows/release.yml` produces artifacts — tag pushed & GPG-signed 2026-09-14; **first release run 34902860370 failed in the sign step** (Linux missing apt deps; Windows MSI rejecting the `-rc1` prerelease id; manifest could clobber `linux-x86_64`) — all fixed in `e785adc`; **re-dispatched via the `workflow_dispatch` tag input → run 34968782545 ALL GREEN 2026-09-15** (windows 31m50s, linux 19m22s, publish 22s). Release `v1.1.0-rc1` live: setup.exe + deb + AppImage + 3 `.sig` sidecars + `latest.json` (both platform keys, non-empty signatures); all three artifacts Ed25519-verify (minisign PreHash) against the runbook §0.1 custody keyid `af58fc682cd6b035`
- [x] `.sig` sidecar emission works with the real release key — `QuantumRAD-Gateway_1.1.0-rc1_amd64.deb.sig` produced locally (minisign, tauri-cli 2.11.4)
- [x] Updater pubkey (`MERCURE_TAURI_PUBLIC_KEY`) is the real release key, not the dev placeholder — secrets `MERCURE_TAURI_PRIVATE_KEY` / `..._PASSWORD` / `..._PUBLIC_KEY` set in the org repo 2026-09-14; private key file SHA-256 matches the runbook §0.1 custody record; **closed by release run 34968782545 (2026-09-15): CI signed the artifacts and all three `.sig` files Ed25519-verify against the §0.1 public key (keyid `af58fc682cd6b035`, minisign PreHash format)**

## 3. Clean-VM UAT (the human step — PRD §9 Phase 1 exit criterion)

- [ ] Windows 10 x64: install → wizard → first study forwarded (K4 ≤ 10 min) — `docs/qa/uat-06.md`
- [ ] Windows 11 x64: same walkthrough
- [ ] Ubuntu (supported LTS): deb + AppImage boot and forward — **dpkg-level ✅ 2026-09-14**: deb installs clean in a `ubuntu:24.04` Docker container (all deps resolve, `quantum-rad-gateway 1.1.0-rc1` reaches `ii` state, `/usr/bin/mercure-gateway` + bundled sidecar resolve, sidecar prints 1.1.0-rc1); GUI boot + tray forward leg still needs a desktop session
- [x] Packaged sidecar spawn verified (tray state transitions idle→sending→error; the one gap CI cannot drive headless) — **2026-09-14 on this box's GNOME session**, deb extracted and run as a normal user with `MERCURE_BACKEND_PORT=18080` (8080 held by another service): sidecar spawned from the fixed deb-layout candidate path, health `1.1.0-rc1`, tray glyph live idle→sending→idle→error (backend killed → poller mapped transport failure), C-STORE of 3 synthetic studies accepted on 11112 and all routed `complete` to Orthanc on first attempt. Found and fixed two product bugs: sidecar path candidate used the binary name instead of productName, and CSP/window plumbing for a non-default port. Visual glyph confirmation (tray icon rendering distinct per state on the real panel) pending user's eyes — appindicator was live in the session.
- [ ] Windows service mode: install/start/stop/uninstall from the admin panel (S07-T9)
- [ ] Auto-update: point a test install at a staging `latest.json`, verify signature enforcement rejects a tampered artifact — **headless/Python half ✅ 2026-09-14** (`scripts/rehearse_updater_tamper.py`: real signed deb served over loopback HTTP; genuine signature staged, bit-flipped signature refused with nothing staged). Tauri in-app half needs the desktop shell on a real install (§3)

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
- PRD §11 external security review — **booking package prepared 2026-09-14**
  (`docs/qa/security-review-package.md` — scope, control/evidence table, declared
  gaps, reviewer quick-start); booking itself = open human step, suggested to
  start after Phase A (remote + CI green). Findings triage gates GA (E2).

## 6. Blocked-by-environment register (what a Windows-VM + GitHub-enabled box must finish)

| Item | Why blocked here | Where to do it |
|---|---|---|
| Windows installer artifact + K6 size row | ~~no Windows OS here~~ **unblocked**: CI K6 gate green (run 34901586844); release artifact awaits re-dispatched run | GH Actions `release.yml` (in progress) / clean VM |
| GH release + `.sig` + merged `latest.json` execution | ~~repo has no remote yet~~ **DONE 2026-09-15**: release 34968782545 green, `v1.1.0-rc1` published with signed assets (§2) | — |
| §3 Windows 10/11 UAT legs, tray/spawn, service mode | Windows hardware | clean VM, `uat-06.md` |
| K9 boot timings + USB flash e2e | physical stick + PC boot matrix — **first candidate stick rejected 2026-09-15: FAKE CAPACITY.** Reported 14.6 GB (USB 2.0 "General UDisk", ROTA=1); flash pipeline ran end-to-end on it (layout + ext4 P1 mounted fine, ≤4 GB region) but 64 MB write→drop_caches→read-back probes proved corruption beyond ~4 GB (0M OK, 4096M/12800M CORRUPT; NTFS/exFAT formatters silently wrote to phantom sectors — mkntfs rc=0 yet boot sector zeroes after cache drop, `invalid boot record signature`). Needs a genuine ≥32 GB USB 3.0 stick per spec | S10 rig, `usb-uat-10.md` |
| loop-device partition e2e (root-only test written) | ~~no root/loop perms~~ **DONE 2026-09-15** (root loop run): uncovered and fixed three shipped-bug classes — sfdisk GPT script syntax (comma-triplet rejected; now key=value + GUIDs + `G` suffix), exFAT label length (>11 chars hard-failed `mkfs.exfat`), and the root-detection gate (`hasattr(Path, "access")` was always False → test could never run anywhere). `test_layout_on_real_loop_device` green under sudo: flash completes, ext4+NTFS+exFAT verified via blkid, data skeleton written | any root Linux box — `test_usb_partition.py::test_layout_on_real_loop_device` |
| CI matrix green record (pip-audit/npm/rustsec via Actions) | ~~no Actions without remote~~ **DONE**: main run 34901586844 all-jobs green 2026-09-14 | — |
