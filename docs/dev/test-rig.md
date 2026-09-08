# Test Rig — Orthanc + Demo Chain (S01-T2)

Stand up a local DICOM test environment for the mercure gateway: Orthanc acts
as both the **test PACS** (C-STORE/C-FIND/C-MOVE target) and the **mercure hub
stand-in** for the Phase 0 demo chain (PRD §9).

## Prerequisites

- Docker + Docker Compose
- `uv` (project uses `uv run`)

## Start the rig

```bash
cd test-rig
make up        # docker compose up -d (Orthanc on 4242 DICOM / 8042 REST)
```

Check it is healthy:

```bash
curl http://localhost:8042/system            # {"Name": "mercure-gateway-test-pacs", ...}
```

## Run the demo chain

The demo chain (`demo/demo_chain.py`) does the full store-and-forward loop in
one process: starts the gateway receiver + forwarder wired to Orthanc-as-hub,
sends a synthetic study from the fake modality, waits for it to be forwarded,
then pulls a report via C-FIND.

```bash
cd test-rig
make demo
```

Expected output:

```
sent: 1 instance(s), 0 failed
forwarded: SENT
report: 1 study(ies) matched via C-FIND
```

Individual steps:

```bash
make send     # fake modality → gateway receiver
make find     # C-FIND report pull against Orthanc
```

## Verify with the Orthanc REST API

Once a study is stored, query it (REST on 8042):

```bash
curl http://localhost:8042/studies
curl "http://localhost:8042/studies?query=1.2.840.10008.99.1"
```

## Components

| Piece | Location | Purpose |
|-------|----------|---------|
| Orthanc (test PACS / hub) | `test-rig/docker-compose.yml`, `test-rig/orthanc.json` | C-STORE/C-FIND/C-MOVE target, AET `ORTHANC` |
| Fake modality SCU | `demo/fake_modality.py` | Generates + C-STORE sends synthetic studies |
| Demo chain runner | `demo/demo_chain.py` | gateway → hub → report pull, one command |
| Report pull C-FIND | `demo/report_pull.py` | C-FIND query of the PACS for study metadata |

## Teardown

```bash
make down
```

## Notes

- Orthanc runs with authentication **disabled** and all DICOM operations
  allowed — for local dev only (never production).
- The gateway destination named `hub` in `demo/demo_chain.py` points at
  `127.0.0.1:4242` AET `ORTHANC`. To use a real mercure hub, change
  `--hub-host/--hub-port/--hub-aet` (S03 forwarder work reuses the same
  `DICOMDestination` shape).
- The fake modality and demo chain are covered by
  `tests/test_fake_modality.py`, `tests/test_demo_chain.py`, and
  `tests/test_report_pull.py` (no Docker required for the tests — they use an
  in-process receiver / C-FIND SCP over localhost).
