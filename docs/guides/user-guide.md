# mercure Gateway — User Guide

## About

mercure Gateway is a lightweight desktop DICOM gateway that receives studies
from modalities (CT/MR scanners), holds them in a local spool, and forwards
them to configured destinations (a PACS, a hub, or a file server). It runs as
a desktop application with a browser-based admin panel.

## Starting the gateway

The gateway starts as a desktop app (Tauri shell) or headless from the command
line. The web admin panel opens at `http://localhost:8080` (browser) and gives
you full control over the device.

## First-time setup (the Wizard)

The first time you open the web admin panel, the **Setup Wizard** guides you
through four steps. Each step must pass its validation gate before you can move
on:

1. **Receiver** — the AE title and port the gateway listens on for incoming
   DICOM. The default AE title is `GATEWAY` on port `11112`. Connect your
   modality to this endpoint.
2. **Destinations** — where received studies should be forwarded. Add one or
   more destinations (DICOM PACS, SFTP, Folder, S3, XNAT, DICOMweb). Each
   destination is verified with a C-ECHO connectivity probe before it is
   accepted.
3. **Reports** — optional report retrieval from a PACS (DICOM SR and PDF
   reports). Configure the report query source if your site uses report
   retrieval.
4. **Summary** — review everything and save.

You can reopen the wizard from the **Setup** tab at any time.

## Daily operations

### Dashboard

The **Dashboard** tab shows live status at a glance:

- **System Status** — Receiver / Forwarder / Report Retriever (running/stopped)
- **Hub Reporting** — registration and event-streaming state (if hub reporting
  is enabled)
- **Queue Overview** — queued / sending / sent / error / failed study counts

### Queue

The **Queue** tab lists every study with its state:

| State | Meaning |
|-------|---------|
| Queued | waiting to be forwarded |
| Sending | delivery in progress |
| Sent | delivered successfully |
| Error | delivery failed, will be retried |
| Failed | delivery failed permanently (retry budget spent) |

Use the state and modality filters to narrow the list. Click a study to see
its destinations; use **Retry** to re-queue a failed study manually.

### Reports

The **Reports** tab lists retrieved report objects (DICOM SR or PDF) with their
status: pending / retrieving / retrieved / failed. Click **Request report** on
a study to pull its report on demand. Retrieved SR reports can be viewed as
structured text in the browser; PDF reports are served as PDF files.

### Audit

The **Audit** tab is a tamper-evident log of every event the gateway emits
(received, forwarded, failed, manual retries, report activity). You can
**Verify** the chain integrity at any time and **Export** the log for offline
storage.

## Receiving studies from your modality

1. Point your modality's DICOM send destination at the gateway: **AE title
   `GATEWAY`, host = this PC, port 11112**.
2. Send a study from the modality.
3. Watch the Dashboard — the study appears in the Queue and is forwarded to
   the configured destinations automatically.

## Troubleshooting (quick)

| Symptom | What to check |
|---------|---------------|
| Modality cannot connect | Receiver port correct? AE title allow-list configured? Modality pointing at the right host? |
| Study stuck in "Queued" | Is the Forwarder running? Is a destination enabled and reachable? |
| Study shows "Failed" | Check the error on the study row; fix the destination and use **Retry**. |
| Web panel won't load | Is the gateway running? Is it on `localhost:8080`? |

For deeper guidance see the **Admin Guide**.
