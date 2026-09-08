# Sprint 07 — v1.1 Destinations, Forwarding Rules & Credential Upgrade (Weeks 13–14)

**Goal:** Broaden the outbound surface: the mercure target-handler family (SFTP, rsync, Folder, S3,
XNAT, DICOMweb) behind the existing `DestinationHandler` protocol, credential storage upgrade to
OS keyring, advanced forwarding rules with a rule tester (US-09), and Windows service mode.

**Exit criteria:** Rules evaluated on tags; multiple targets per rule; priority respected; rule
tester included (US-09 AC); new destinations deliver through the existing forwarder unchanged;
OS keyring credential storage working; Windows service mode available.

**PRD refs:** §2.3 v1.1, §5.5 `forwarding_rules`, §6.2 credentials, §8.3 reuse, §14 US-09.
**Refinement refs:** product-refinement-spec.md §3.2 (v1.1 scope), §2.5 (OS keyring deferred here).

**Per-sprint gate:** `config/__init__.py` already models all 8 destination types (11 config tests)
— handlers are the missing half. S04-T5's encrypted config credential store gets its first real
consumers here; S07-T8 upgrades to OS keyring.

| ID | Task | PRD ref | RED → GREEN | DoD | Status |
|----|------|---------|-------------|-----|--------|
| S07-T1 | **Handler contract consolidation:** freeze `DestinationHandler` as the mercure target-handler boundary (docs + protocol polish + base-class helpers for retries/paths) | §8.3 | `tests/test_handler_contract.py` additions — handler registration/dup/unknown-type semantics pinned | Contract stable for new handlers | ✅ (test_handler_contract.py, 6 new — 347 total) |
| S07-T2 | **Folder handler:** deliver study dir copy/move to filesystem target | §2.3 | `tests/test_folder_handler.py` — copy semantics, existing-dir error, path templates | Simplest non-DICOM handler green | ✅ (test_folder_handler.py, 6 new; folder.py handler implemented; path templates, 347 total) |
| S07-T3 | **SFTP handler + encrypted config creds:** paramiko SFTP delivery pulling credentials via S04-T5 encrypted config store | §2.3, §6.2 | `tests/test_sftp_handler.py` — against local SFTP test server (docker on rig); key-based auth from encrypted config; failure → `DeliveryResult` error | §6.2 creds consumed end-to-end | ✅ (test_sftp_handler.py, 5 new — 355 total; paramiko handler + credential-vault Protocol; deps: paramiko, pylibjpeg-rle for F7 compressed delivery) |
| S07-T4 | **rsync + S3 + XNAT handlers:** per-target delivery; S3 via boto3, XNAT via REST upload | §2.3, §8.3 | `tests/test_rsync_handler.py`, `tests/test_s3_handler.py` (moto), `tests/test_xnat_handler.py` (fake XNAT) | Full §2.3 v1.1 destination set green | ✅ (test_rsync_handler.py 4, test_s3_handler.py 4, test_xnat_handler.py 4 — 367 total; all handlers registered; deps: boto3, requests) |
| S07-T5 | **DICOMweb (STOW-RS) handler:** HTTPS STOW destination; TLS/HTTPS per §6.1 | §2.3, §6.1 | `tests/test_dicomweb_handler.py` — STOW to fake server; TLS on; error mapping | DICOMweb destination green | ✅ (test_dicomweb_handler.py, 4 new — 371 total) |
| S07-T6 | **Forwarding rules engine:** evaluate rules on extracted tags (`*.tags` from S02-T4); targets + priority per rule; default route when no rule matches | §5.5, US-09 | `tests/test_rules.py` — rule match/no-match/priority ordering; multi-target expansion; invalid rule rejected | US-09 rules AC green | ✅ (test_rules.py, 10 new — 385 total; `src/mercure_gateway/rules.py` RuleEngine; also fixed pre-existing review F9 hotplug false-removal bug + RLE codec deps) |
| S07-T7 | **Rule tester:** service + UI surface to run a tag-set against configured rules and preview targets | US-09 | `tests/test_rule_tester.py` — deterministic preview given synthetic tag sets | Rule tester AC green | ✅ (test_rule_tester.py, 6 new — 391 total; `src/mercure_gateway/rules_tester.py`) |
| S07-T8 | **OS keyring credential storage:** upgrade from encrypted config file to OS keyring (`keyring` lib); Windows Credential Manager on Windows; SecretService on Linux; fallback to encrypted config when keyring unavailable; credentials migrated transparently | §6.2, refinement §3.2 | `tests/test_keyring_credentials.py` — set/get/delete round-trip on keyring; fallback path when no keyring; migration from encrypted config | OS keyring working; encrypted config remains fallback | ✅ (test_keyring_credentials.py, 4 new — 395 total; `src/mercure_gateway/keyring_store.py` with FailKeyring availability detection + fallback cache; deps: keyring) |
| S07-T9 | **Windows service mode:** gateway runs as a Windows service (via `pywin32` or NSSM); tray icon still available; service can be started/stopped from web admin panel; auto-start on boot via service | §13 Q3, refinement §3.2 | `tests/test_windows_service.py` — service lifecycle (start/stop/restart); web UI can manage service; tray + service coexist | Windows service mode available as alternative to tray | ✅ (test_windows_service.py, 6 new — 401 total; `src/mercure_gateway/service_controller.py` idempotent lifecycle + `service_backend.py` pywin32 backend via importlib; pywin32 backend needs a Windows VM to run end-to-end) |

**Evidence:** _(links to commits/PRs when done)_

**Notes:**
- Handler RED tests run against local fakes (SFTP server container, moto for S3, fake XNAT/STOW
  endpoints) per the §10 integration strategy; the rig gains these services in T2–T5 setup tasks.
- This sprint adds runtime deps (paramiko, boto3) — watch K6 installer size in CI and note deltas
  in the sprint Evidence line.
- **Refinement change (S07-T8):** The original spec used OS keyring from day one. The refinement
  defers OS keyring to v1.1 (this sprint) because air-gapped sites may not have keyring available.
  S04-T5's encrypted config file is the MVP credential store; S07-T8 upgrades to OS keyring when
  available, with transparent fallback.
- **Refinement change (S07-T9):** Windows service mode is confirmed for v1.1. The tray app remains
  the default (MVP), but users can optionally run as a Windows service for always-on headless
  operation. The web admin panel (S06) can manage the service.
