# ADR-0006: Application Auto-Update — Update Channel & Signing Approach

**Status:** Accepted
**Date:** 2026-08-30
**Deciders:** Product + Engineering
**Relates to:** PRD §13 (Q5), Sprint 08 S08-T6 (feeds S09-T4 implementation)

## Context

PRD §13 Q5 asks how the desktop gateway receives updates in the field. The gateway ships as a
Tauri v2 desktop application (ADR-0002) with a Windows installer and, from Sprint 08, Linux
AppImage/deb artifacts. Options considered:

1. **Tauri Updater** (`@tauri-apps/plugin-updater` + `tauri.conf.json` `plugins.updater`).
   Built-in Tauri mechanism: the app fetches a signed JSON manifest from a static endpoint,
   compares the published version, downloads the signed archive/installer, verifies the Ed25519
   signature, and swaps the binary on restart. Supported for Windows and Linux.
2. **Platform-native auto-update** (Squirrel.Windows / winget on Windows, AppImageUpdate /
   snap-store on Linux). Higher fidelity per platform but two divergent code paths, and no
   shared signing story across OSes.
3. **Manual download only** — no in-app updater; operators fetch new installers from a release
   page. Simplest, but leaves field devices on stale versions (a clinical-compliance concern).

## Decision

**Use Tauri Updater with a single update channel and Ed25519-signed artifacts.**

Rationale:

- **One code path across Windows + Linux.** Tauri Updater covers both target platforms in
  Sprint 08's CI matrix; Squirrel/snap would fork the update logic per OS.
- **Signature verification is built in.** Each update is signed with an Ed25519 key; the
  updater rejects any archive whose signature does not verify, so a compromised or poisoned
  update endpoint cannot push unsigned binaries to clinical devices.
- **Static manifest + private key custody.** The update manifest (`latest.json`) is served from
  a static release endpoint; the private signing key is held in the release pipeline's secret
  store (never shipped, never committed — matches the project's secret-hygiene rules).

## Consequences

- `tauri.conf.json` will enable `plugins.updater` with `endpoints` pointing at the release
  manifest and `pubkey` embedding the Ed25519 **public** key (S09-T4).
- The CI release workflow signs the Windows installer and Linux AppImage/deb with the private
  key and publishes `latest.json` plus artifacts (extends the Sprint 08 packaging matrix).
- Updates are **opt-in at startup** (a background check, never a forced restart mid-forward),
  preserving the gateway's non-blocking availability guarantees.
- Operators can still disable the updater via configuration for environments that pin versions
  (e.g. site-validated builds) — update checks are gated behind a config flag.
