# Packaging pipeline — frozen backend + Tauri bundles + auto-update (WS1)

The desktop app is a Tauri shell around a frozen Python backend. This doc is
the operator guide for the whole chain: freeze → bundle → sign → release.

## Why resources, not `externalBin`

Tauri v2's `bundle.externalBin` appends a target triple to a **single file**
and fails the build for directories (tauri-apps/tauri#6676, #7318). The
backend is a PyInstaller **onedir** bundle (hundreds of files). Two options
existed:

1. **onefile** — a single self-extracting binary that would ride in
   `externalBin` directly, but extracts to a temp dir on *every* launch
   (slow start for an auto-start clinical appliance, antivirus-hostile).
2. **onedir via `bundle.resources` + explicit-path spawn** (chosen) — fast
   startup, AV-friendly; `src-tauri/src/lib.rs` resolves the backend path
   from a candidate list (`backend_candidates()`) and spawns it with
   `app.shell().command(path)`.

The dev fallback candidate lets `cargo tauri dev` work from the manifest dir.

## Build chain

```bash
# 1. Freeze the backend (PyInstaller onedir → dist/, copy → src-tauri/binaries/)
uv run python scripts/package_backend.py

# 2. Build the desktop app (bundles the binaries/ dir as a resource)
cd src-tauri && cargo tauri build
```

For local development without the frozen bundle:

```bash
uv run python scripts/tauri_placeholders.py   # stub marker file
cd src-tauri && cargo check && cargo clippy -- -D warnings
```

`scripts/tauri_placeholders.py` writes a zero-byte marker at
`src-tauri/binaries/mercure-gateway/mercure-gateway(.exe)` so tooling that
expects bundle inputs is well-defined. The stub is never shipped.

PyInstaller lives in the `package` dependency extra:

```bash
uv sync --extra package
```

## K6 size gates

The installer gate (≤ 250 MB, PRD K6) covers the shell binary **plus** the
backend bundle. `scripts/package_backend.py` prints the onedir size; the CI
Windows job sums shell exe + bundle before comparing against the gate.

## Auto-update (ADR-0006)

- `plugins.updater` in `tauri.conf.json` points at the release manifest
  (`latest.json`) and embeds the **public** Ed25519 key.
- The committed `pubkey` is a placeholder; the real key is injected at
  release time via `cargo tauri build --config src-tauri/tauri.release.conf.json`
  (a JSON-merge overlay templated from the `MERCURE_TAURI_PUBLIC_KEY`
  secret). Public keys are not secrets, but keeping the release key out of
  dev builds avoids accidentally trusting a test endpoint.
- Capabilities: `updater:default` + `process:default` are granted; the SPA
  update banner is a deferrable slice — the Python-side log-only startup
  check (`main._check_for_updates`) already satisfies ADR-0006's opt-in
  minimum.

### Signing key custody (document-only; never generated in CI)

```bash
# One time, on a maintainer machine:
cargo tauri signer generate -w ~/.tauri/mercure-gateway.key
# GitHub secrets:
#   MERCURE_TAURI_PRIVATE_KEY      contents of mercure-gateway.key
#   MERCURE_TAURI_KEY_PASSWORD     (if set at generation)
#   MERCURE_TAURI_PUBLIC_KEY       contents of mercure-gateway.key.pub
```

`cargo tauri build` with `TAURI_SIGNING_PRIVATE_KEY[_PASSWORD]` in the
environment emits `.sig` sidecars next to the artifacts; the release
workflow assembles `latest.json` from them (see
`.github/workflows/release.yml`).

## CI packaging jobs

`package-windows` and `package-linux` both:

1. `uv run python scripts/package_backend.py` (freeze + copy),
2. `cargo tauri build …` (bundle),
3. enforce the K6 gate,
4. **smoke-test the frozen backend**: launch
   `dist/…/mercure-gateway --web --port 8099`, poll
   `GET /api/system/health` until `{"status":"ok"}` (≤ 30 s), kill. This is
   what catches missing uvicorn hidden imports / static-asset mount issues.

The packaged sidecar spawn (Rust resource-path resolution inside the real
bundle) cannot run headless in CI — that check is the clean-VM UAT step in
`docs/qa/rc1-checklist.md`.
