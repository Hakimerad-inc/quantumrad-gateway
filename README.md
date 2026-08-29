# mercure Gateway

A lightweight desktop DICOM gateway — acts as a local DICOM ingestion node,
forwards studies to a central mercure hub or vendor PACS, and retrieves study
reports from PACS.

## Quick start

```bash
uv sync
uv run mercure-gateway --help
uv run pytest
```

## Architecture

See [mercure-gateway-PRD.md](./mercure-gateway-PRD.md) for the full product
specification, including the data flow (§3), database schema (§5.4), and
configuration model (§5.5).

```
  Modality (C-STORE) → Receiver (SCP) → Spool (SQLite+files)
                                           ↓
  PACS report      ← ReportRetriever    Forwarder → DICOM / hub / other
                                           ↓
                                       AuditLog (chained hash)
```

## License

MIT — consistent with [mercure](https://github.com/mercure-imaging/mercure).