# Sprint 07 — v1.1 Destinations + Forwarding Rules (Weeks 13–14)

**Goal:** Broaden the outbound surface: the mercure target-handler family (SFTP, rsync, Folder, S3,
XNAT, DICOMweb) behind the existing `DestinationHandler` protocol, credential storage integration,
and advanced forwarding rules with a rule tester (US-09).

**Exit criteria:** Rules evaluated on tags; multiple targets per rule; priority respected; rule
tester included (US-09 AC); new destinations deliver through the existing forwarder unchanged.

**PRD refs:** §2.3 v1.1, §5.5 `forwarding_rules`, §6.2 credentials, §8.3 reuse, §14 US-09.

**Per-sprint gate:** `config/__init__.py` already models all 8 destination types (11 config tests)
— handlers are the missing half. S04-T5's credential store gets its first real consumers here.

| ID | Task | PRD ref | RED → GREEN | DoD | Status |
|----|------|---------|-------------|-----|--------|
| S07-T1 | **Handler contract consolidation:** freeze `DestinationHandler` as the mercure target-handler boundary (docs + protocol polish + base-class helpers for retries/paths) | §8.3 | `tests/test_forwarder.py` additions — handler registration/dup/unknown-type semantics pinned | Contract stable for new handlers | ☐ |
| S07-T2 | **Folder handler:** deliver study dir copy/move to filesystem target | §2.3 | `tests/test_folder_handler.py` — copy semantics, existing-dir error, path templates | Simplest non-DICOM handler green | ☐ |
| S07-T3 | **SFTP handler + keyring creds:** paramiko SFTP delivery pulling credentials via S04-T5 store | §2.3, §6.2 | `tests/test_sftp_handler.py` — against local SFTP test server (docker on rig); key-based auth from keyring; failure → `DeliveryResult` error | §6.2 creds consumed end-to-end | ☐ |
| S07-T4 | **rsync + S3 + XNAT handlers:** per-target delivery; S3 via boto3, XNAT via REST upload | §2.3, §8.3 | `tests/test_rsync_handler.py`, `tests/test_s3_handler.py` (moto), `tests/test_xnat_handler.py` (fake XNAT) | Full §2.3 v1.1 destination set green | ☐ |
| S07-T5 | **DICOMweb (STOW-RS) handler:** HTTPS STOW destination; TLS/HTTPS per §6.1 | §2.3, §6.1 | `tests/test_dicomweb_handler.py` — STOW to fake server; TLS on; error mapping | DICOMweb destination green | ☐ |
| S07-T6 | **Forwarding rules engine:** evaluate rules on extracted tags (`*.tags` from S02-T4); targets + priority per rule; default route when no rule matches | §5.5, US-09 | `tests/test_rules.py` — rule match/no-match/priority ordering; multi-target expansion; invalid rule rejected | US-09 rules AC green | ☐ |
| S07-T7 | **Rule tester:** service + UI surface to run a tag-set against configured rules and preview targets | US-09 | `tests/test_rule_tester.py` — deterministic preview given synthetic tag sets | Rule tester AC green | ☐ |

**Evidence:** _(links to commits/PRs when done)_

**Notes:**
- Handler RED tests run against local fakes (SFTP server container, moto for S3, fake XNAT/STOW
  endpoints) per the §10 integration strategy; the rig gains these services in T2–T5 setup tasks.
- This sprint adds runtime deps (paramiko, boto3) — watch K6 installer size in CI and note deltas
  in the sprint Evidence line.
