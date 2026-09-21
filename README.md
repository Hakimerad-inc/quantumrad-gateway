# mercure Gateway

A lightweight desktop DICOM gateway. It acts as a local ingestion node —
modalities push studies to a store-and-forward receiver, the gateway holds them
in a local spool, and a forwarder delivers them to a central mercure hub or a
vendor PACS, with reports retrieved back from the PACS. Every step is written
to a chained-hash audit log.

The gateway ships as a **Tauri desktop app** (a Rust shell wrapping a bundled
React admin panel and a frozen Python backend), and can also run **headless**
as a systemd service. Both forms expose the same web admin panel on
`http://127.0.0.1:8080` by default.

## For operators

If you are deploying or running the gateway, start with the user guide and the
site deployment runbook; the guides below cross-reference each other.

| Doc | What it covers |
| --- | --- |
| [docs/guides/user-guide.md](./docs/guides/user-guide.md) | Operator walkthrough — starting the gateway and the first-time setup wizard. |
| [docs/guides/site-deployment.md](./docs/guides/site-deployment.md) | End-to-end site deployment runbook, from network pre-flight to a signed-off soak. Covers the desktop and headless forms. |
| [docs/guides/admin-guide.md](./docs/guides/admin-guide.md) | Configuration, security, retention, and troubleshooting. |
| [docs/guides/secrets-and-env-overrides.md](./docs/guides/secrets-and-env-overrides.md) | Every credential the gateway holds, where it lives, and how to inject secrets without writing them to a config file. |
| [docs/guides/backup-restore.md](./docs/guides/backup-restore.md) | What state lives on disk, and how to back it up and restore it without breaking the audit chain. |
| [docs/guides/migration-guide.md](./docs/guides/migration-guide.md) | What changes for a deployed appliance when upgrading to v1.1.0 GA. |
| [docs/guides/usb-quickstart.md](./docs/guides/usb-quickstart.md) | The portable USB-dongle variant (boot-from-USB on Linux, or plug-and-run on Windows). |
| [systemd/README.md](./systemd/README.md) | Installing the gateway as a hardened, self-healing systemd user service (headless deployments). |

## For developers

```bash
uv sync
uv run mercure-gateway --help
uv run pytest
```

For the full development environment — the SPA, its type gate, local
pre-commit hooks, and the two ways to invoke the test suites that are easy
to get wrong — see [docs/dev/setup.md](./docs/dev/setup.md).

If you have [just](https://github.com/casey/just) installed, `just setup &&
just test && just lint` runs the same gates CI does.

| Doc | What it covers |
| --- | --- |
| [mercure-gateway-PRD.md](./mercure-gateway-PRD.md) | Product specification — data flow (§3), database schema (§5.4), configuration model (§5.5). |
| [product-refinement-spec.md](./product-refinement-spec.md) | Refinement of the PRD's open design questions. |
| [docs/dev/packaging.md](./docs/dev/packaging.md) | How the Python backend is frozen and bundled into the desktop installer. |
| [docs/dev/release-runbook.md](./docs/dev/release-runbook.md) | How a release is published, signed, and rolled back. |
| [docs/dev/test-rig.md](./docs/dev/test-rig.md) | The external test rig — an Orthanc instance as the mercure hub stand-in, plus demo modality scripts. |
| [docs/dev/hub-anchor-api.md](./docs/dev/hub-anchor-api.md) | The hub anchor API the gateway registers audit anchors against. |
| [CHANGELOG.md](./CHANGELOG.md) | What changed in each release. |

## Architecture

The packaged app is three layers. The **Tauri shell** (`src-tauri/`, Rust) owns
the window, the tray icon, and the update channel; it spawns the **Python
backend** as a sidecar at startup. The **admin panel** is a React SPA built by
Vite from `web/` into `src/mercure_gateway/web/static`, and is served two ways:
the shell loads it from its own embedded asset origin, and the Python backend
mounts the same files at `/`.

```
  ┌──────────────────────────────────────────────────────────────────┐
  │  Tauri shell  (Rust, src-tauri/)                                  │
  │                                                                  │
  │    Tray icon: idle / sending / error / removable                  │
  │                                                                  │
  │    Webview → embedded SPA (React, the Vite build),                │
  │      loaded from the shell's own asset origin                     │
  │          │  fetch http://127.0.0.1:8080/api/...  (loopback)       │
  └──────────┼──────────────────────────────────────────────────────────┘
             ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │  Python sidecar  (src/mercure_gateway/, frozen as a PyInstaller   │
  │  onedir and bundled as a shell resource)                          │
  │                                                                  │
  │    FastAPI web layer  ── also serves the SPA at "/"                │
  │                                                                  │
  │    Modality ──C-STORE──▶ Receiver (SCP) ──▶ Spool (SQLite + files)│
  │                                                  │               │
  │    PACS report ◀── ReportRetriever          Forwarder ──▶ PACS /  │
  │                                                  │          hub   │
  │                                             AuditLog             │
  │                                             (chained hash)       │
  └──────────────────────────────────────────────────────────────────┘
```

The same Python process runs headless without the shell — the systemd unit
starts it directly, and the browser loads the panel from `/`.

## Decision records

| ADR | Decision |
| --- | --- |
| [ADR-0001](./docs/adr/ADR-0001-receiver-transport-abstraction.md) | pynetdicom is the C-STORE SCP transport for the MVP, behind a `ReceiverTransport` protocol so DCMTK stays a drop-in fallback. |
| [ADR-0002](./docs/adr/ADR-0002-desktop-shell-architecture.md) | The SPA is bundled into the Tauri binary and the FastAPI backend is a sidecar the shell spawns (Method 2, amended 2026-09-21). |
| [ADR-0003](./docs/adr/ADR-0003-license-and-hub-contract.md) | MIT licence; the hub bookkeeper contract is deferred and proven against a stub. |
| [ADR-0004](./docs/adr/ADR-0004-at-rest-encryption.md) | At-rest encryption strategy, including the key-required guard that refuses to open a keyed database without the right key. |
| [ADR-0005](./docs/adr/ADR-0005-report-retrieval-strategy.md) | Retrieve SR and encapsulated PDF via C-FIND/C-MOVE first; DICOMweb is additive, not a replacement. |
| [ADR-0006](./docs/adr/ADR-0006-auto-update.md) | Tauri Updater with Ed25519-signed manifests, one code path across Windows and Linux. |
| [ADR-0007](./docs/adr/ADR-0007-web-admin-transport-posture.md) | The admin panel binds loopback by default with a refusal boundary; TLS is opt-in. |

## License

MIT — consistent with [mercure](https://github.com/mercure-imaging/mercure).
See [LICENSE](./LICENSE) and [ADR-0003](./docs/adr/ADR-0003-license-and-hub-contract.md).
