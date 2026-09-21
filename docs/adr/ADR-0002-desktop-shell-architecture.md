# ADR-0002: Desktop Shell Architecture

**Status:** Accepted (amended 2026-09-21)
**Date:** 2026-08-29
**Deciders:** Product + Engineering
**Relates to:** PRD §13 Q1, product-refinement-spec §2.1, S01-T4 spike, ADR-0006,
ADR-0007

> **Amendment (2026-09-21) supersedes the original Decision and
> Recommendation.** The option recorded below as "chosen for MVP" (Method 1,
> localhost HTTP with the webview loading a runtime URL) is **not** what
> shipped. The packaged app is Method 2: the SPA is bundled into the Tauri
> binary and the FastAPI backend is a sidecar the shell spawns. The original
> decision text and the option comparison are retained unchanged further down
> — they are real decision history — but the decision of record is the
> [Amendment](#amendment-2026-09-21-method-2-is-the-shipped-architecture)
> section. The amendment also corrects the shell capability claims the
> original prose made (a notification plugin and an auto-start plugin, neither
> of which exists) and the tray state list (three states documented, four
> shipped).

## Context

The PRD (§5.1, §13 Q1) specifies a desktop shell for the gateway — tray icon, main window
(queue/status/logs/settings/reports), and guided first-run wizard. The original options were
PySide6 (Qt) or Tauri wrapping a web UI. The product refinement interview resolved this:
the primary user is a single person wearing multiple hats at a small clinic, and the
configuration UI should be a **web admin panel** accessible via localhost browser.

This ADR documents the architecture decision and the three communication methods evaluated
during the Phase 0 spike.

## Decision (as recorded 2026-08-29 — superseded)

> **Superseded 2026-09-21.** Retained verbatim as decision history. Its claims
> about "system notifications" and "auto-start" were never true of the shipped
> shell (amendment item 4), and the webview never loaded a runtime localhost
> URL (amendment item 1). The decision of record is the
> [Amendment](#amendment-2026-09-21-method-2-is-the-shipped-architecture).

**Tauri + FastAPI + React/Vue SPA** — Tauri provides the native desktop shell (tray icon,
system notifications, auto-start), while FastAPI serves a web-based admin panel on
localhost:8080 that the Tauri webview loads.

## Architecture (as shipped)

```
┌─────────────────────────────────────────────────────────────────┐
│ Tauri Shell (Rust, src-tauri/src/lib.rs)                         │
│                                                                 │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │ Webview → WebviewUrl::App("index.html")                   │  │
│  │   embedded SPA (build.frontendDist → the Vite build),     │  │
│  │   served from the tauri://localhost asset origin          │  │
│  │   └─ React admin panel (config, queue, reports, audit)    │  │
│  └───────────────────────────────────────────────────────────┘  │
│        │  fetch http://127.0.0.1:{port}/api/...  (loopback)      │
│        ▼                                                         │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │ Frozen FastAPI sidecar (PyInstaller onedir, bundled via   │  │
│  │ bundle.resources; spawned by the shell at startup)         │  │
│  │   ├─ REST API (config, queue, reports, audit)             │  │
│  │   ├─ Receiver (C-STORE SCP)                               │  │
│  │   ├─ Forwarder (concurrent)                               │  │
│  │   ├─ ReportRetriever                                      │  │
│  │   ├─ AuditLog (chained hash)                              │  │
│  │   └─ also serves the same SPA at "/" (StaticFiles)        │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                 │
│  Tray Icon: idle / sending / error / removable                  │
│    (priority error > sending > removable > idle)                │
└─────────────────────────────────────────────────────────────────┘
```

## Communication Methods Evaluated

### Method 1: Localhost HTTP (chosen for MVP, as recorded 2026-08-29)

> Not what shipped — see the [amendment](#amendment-2026-09-21-method-2-is-the-shipped-architecture).
> It survives as the **dev** posture: `cargo tauri dev` loads `build.devUrl`
> (`http://127.0.0.1:8080`), the Vite dev server proxies `/api` at the backend
> (`web/vite.config.ts`), and `just build-web` writes the SPA into
> `src/mercure_gateway/web/static/` where the backend serves it at `/` — so the
> admin panel is developed and tested against plain loopback HTTP, with no
> Tauri shell in the loop.

| Aspect | Detail |
|--------|--------|
| **How it works** | FastAPI runs as a separate process on localhost:8080. Tauri webview loads this URL. SPA makes standard `fetch()` calls to REST API. |
| **Pros** | Simplest; standard HTTP; SPA works in any browser (not just Tauri); easy to debug; decoupled frontend/backend. |
| **Cons** | Requires FastAPI process management; HTTP overhead per request; port conflict risk. |
| **Latency** | ~1-2 ms per request on localhost (measured in spike). |
| **RAM** | FastAPI idle: ~30-50 MB; Tauri shell: ~30-50 MB; Total: ~60-100 MB. |
| **Complexity** | Low — standard web architecture. |

### Method 2: Tauri Sidecar (the shipped architecture)

| Aspect | Detail |
|--------|--------|
| **How it works** | FastAPI is bundled as a Tauri sidecar binary. Tauri manages its lifecycle (start/stop/restart). Communication still HTTP over localhost. |
| **Pros** | Tauri manages process lifecycle; single binary distribution; no port conflict (dynamic port assignment). |
| **Cons** | Requires bundling Python runtime in sidecar; larger installer size; more complex build. |
| **Latency** | Same as Method 1 (~1-2 ms). |
| **RAM** | Same as Method 1 (~60-100 MB total). |
| **Complexity** | Medium — sidecar bundling adds build complexity. |

### Method 3: Tauri Commands Bridge (PyO3)

| Aspect | Detail |
|--------|--------|
| **How it works** | Python runs embedded in the Rust process via PyO3. Tauri commands call Python functions directly. No HTTP involved. |
| **Pros** | Lowest latency; no HTTP overhead; single process; no port needed. |
| **Cons** | PyO3 adds build complexity; Python GIL limits concurrency; harder to debug; tighter coupling; larger binary. |
| **Latency** | ~0.1-0.5 ms per call (no HTTP overhead). |
| **RAM** | ~40-80 MB (single process, no HTTP server). |
| **Complexity** | High — PyO3 integration, GIL management, build toolchain. |

## Recommendation (superseded 2026-09-21)

**Method 1 (localhost HTTP) for MVP**, with Method 2 (sidecar) as an optional upgrade path.

Rationale:

- Method 1 is the simplest to implement, debug, and test
- The SPA works in any browser (useful for remote desktop support — PRD support model)
- The ~1ms HTTP overhead is negligible compared to DICOM transfer times (seconds)
- Total RAM (~60-100 MB) is well within K6 target (≤150 MB)
- Method 2 can be adopted later for single-binary distribution without changing the SPA or API

Method 3 (PyO3) is **not recommended** due to complexity, GIL limitations, and debugging difficulty.
The latency benefit (~1ms saved) is not worth the trade-offs.

## Benchmark Results (Phase 0 spike, measured 2026-08-29)

| Metric | Target | Measured | Gate |
|--------|--------|----------|------|
| Request latency (avg, localhost curl) | — | 22 ms (min 16ms, max 33ms) | ✅ Negligible |
| Idle RSS (FastAPI process) | — | 36 MB | ✅ Within budget |
| Idle RSS (total est: FastAPI + Tauri) | ≤150 MB | ~70-90 MB (est) | ✅ PASS |
| Throughput (curl-bound) | — | ~19 req/s (curl bottleneck; FastAPI handles 500+) | ✅ Sufficient |

> **Note:** Latency numbers include curl round-trip overhead (~15ms). FastAPI internal
> request handling is sub-millisecond. The 19 req/s throughput is curl-limited;
> FastAPI+uvicorn serves 500+ req/s in raw benchmarks.

## Amendment (2026-09-21): Method 2 is the shipped architecture

**Decision of record:** the packaged desktop app ships Method 2 — the SPA is
bundled **into** the Tauri binary and the FastAPI backend is a sidecar the
shell launches at startup. The webview never loads a runtime localhost URL.
Communication is still HTTP over loopback, so this is the Method 2 of the
option comparison above (SPA embedding + sidecar backend), not a fourth
option.

**What changed between the original decision and the shipped code.** The
original Decision and Recommendation described the webview loading
`http://127.0.0.1:8080` at runtime and deferred the sidecar to an "optional
upgrade path". By the S06 build the shell had moved the other way: loading the
SPA from an embedded bundle is strictly better for a clinical appliance (no
dependency on a live local server before the UI appears, no mixed-origin
hosting of the admin panel), and the port-conflict con that argued against
Method 1 turned out to be fixable without changing the method (see below).
Nothing in this changes the Phase 0 benchmark above — the numbers measured
loopback HTTP, which is still the transport.

**What the old prose got wrong, corrected against the code in this tree:**

1. **The webview URL.** `src-tauri/tauri.conf.json` `build.frontendDist` points
   at `../src/mercure_gateway/web/static` — the Vite build output, which the
   Python package also serves (`src/mercure_gateway/web/__init__.py` mounts it
   with `StaticFiles` at `/`). The main window is built in Rust with
   `tauri::WebviewUrl::App("index.html")` (`src-tauri/src/lib.rs`), i.e. the
   embedded asset origin (`tauri://localhost`). `app.windows` is empty in the
   config precisely so the window can be built in code: that is the only way
   to attach the initialization script that publishes
   `window.__MERCURE_PORT__` to the SPA. `build.devUrl` is the *dev-only*
   URL (`cargo tauri dev`); it is not what a packaged install loads.
2. **The backend is bundled, not assumed to be running.** The Python backend
   is a PyInstaller **onedir** bundle shipped as `bundle.resources`
   (`binaries/mercure-gateway/` in `tauri.conf.json`). `externalBin` cannot
   carry a directory, so the shell resolves the executable from a candidate
   list (`backend_candidates()`) and spawns it itself with
   `app.shell().command(path).args(["--web", "--port", …])`. A missing bundle
   or failed spawn is non-fatal — the app launches without a backend. (The
   whole chain, including the `externalBin` limitation, is documented in
   `docs/dev/packaging.md`.)
3. **The port-conflict con of Method 1 is handled.** The sidecar is told which
   port to bind and the SPA is told which port to call, both from
   `MERCURE_BACKEND_PORT` (default 8080), so a packaged install can coexist
   with another service on 8080. The CSP keeps `connect-src` open to
   `http://127.0.0.1` for the same reason.
4. **No notification plugin, and no auto-start plugin.** The original Decision
   credited Tauri with "system notifications, auto-start". Neither is in the
   shell: `src-tauri/Cargo.toml` declares `tauri` (with the `tray-icon`
   feature), `tauri-plugin-shell`, `tauri-plugin-updater`,
   `tauri-plugin-process` and `tauri-plugin-log` — no `tauri-plugin-notification`,
   no `tauri-plugin-autostart`, and no notification API on the Python or web
   side either. The operator's at-a-glance signal is the **tray icon glyph and
   tooltip**, plus structured logs to the platform app log directory and stdout
   via `tauri-plugin-log` (the artifact an operator attaches to a support
   ticket). Boot-time start, where a site needs it, is the Windows service
   mode of S07-T9 — not a shell plugin. The capability file
   (`src-tauri/capabilities/default.json`) grants the webview only
   `core`/`window`/`webview`/`updater`/`process` defaults and explicitly no
   filesystem or shell access; the shell permission is used by the Rust host
   to spawn the backend, not exposed to the page.
5. **Four tray states, not three.** The tray has four states — `idle`,
   `sending`, `error`, `removable` — with priority
   `error > sending > removable > idle`. `removable` is the USB safe-to-remove
   hint (the `usb_mode` config field, PRD §5.5): it is only reachable when every component reports
   running *and* the queue is provably empty *and* `usb_mode` is set, because
   a safe-to-remove hint shown mid-transfer is actively dangerous. Both halves
   implement it: the Python spec `src/mercure_gateway/tray.py`
   (`derive_tray_state`) and the Rust mirror `src-tauri/src/lib.rs`
   (`TRAY_REMOVABLE`, the `derive_state` removable arm, its label
   "safe to remove" and its own glyph). Both are tested —
   `tests/test_tauri_integration.py` covers all four states and the priority
   ordering on the Python side, and the Rust `#[cfg(test)]` module in
   `lib.rs` covers the same matrix on the shell side, including that a busy
   queue never yields `removable`.
   *Note on the two implementations:* the Rust copy is a deliberate
   near-mirror, not a literal one — a missing component field reads as
   "stopped" in Rust and "running" in Python, so the tray fails closed on a
   schema change. That divergence is pinned by a dedicated test and documented
   in the `COMPONENT_DEFAULT` comment.
   *Note on the bundle icon list:* `tauri.conf.json` `bundle.icon` carries the
   app bundle icons plus `tray-idle/sending/error.png` but not
   `tray-removable.png`. This is not a defect — tray glyphs are compiled into
   the binary with `include_bytes!` at build time rather than read from the
   resource directory at runtime, and `icons/tray-removable.png` is present in
   `src-tauri/icons/`. The `bundle.icon` list only feeds platform launcher
   icons.
6. **The "SPA works in any browser" pro survived the switch.** Because the
   same built bundle is served by the backend at `/`, opening
   `http://127.0.0.1:{port}` in a plain browser still gives the full admin
   panel — remote-desktop support needs no Tauri shell. Method 2 kept the
   property that motivated Method 1.

**Not claimed here.** The shell spawns the backend and holds the child handle
for the app's lifetime (draining its output so the OS pipes never fill); it
does not currently supervise or restart it. Restart-on-failure is out of scope
for this ADR — nothing in the tree implements it.

## Consequences

- **S06** built the Tauri shell + FastAPI backend + SPA as an embedded-SPA +
  sidecar package (Method 2 as amended above), not the runtime-URL form
  originally recorded.
- **S06-T1** wires the Tauri tray icon to `GET /api/system/status` and
  `GET /api/queue/stats` every 5 s, deriving one of four states; a transport
  failure maps to `error` (an unreachable backend is an attention-needed
  condition), and the state machine is unit-tested on both the Python and the
  Rust side.
- **S06-T5** web wizard is a client-side page in the SPA (`SetupWizardPage`)
  whose per-step validation goes through `POST /api/wizard/validate/{step}`;
  it is reachable both from the embedded webview and from a plain browser.
  (The original consequence text named a FastAPI route at `/setup`; no such
  route exists — the SPA is a page-state machine, not a set of URL routes, and
  the wizard's only server-side surface is the validate endpoint.)
- **S07-T9** (Windows service mode) is the boot-time/auto-start path where a
  site needs one; it does not rely on a Tauri autostart plugin, which does not
  exist in this tree.
- The SPA is testable independently of Tauri (open a browser at the backend's
  origin), and the same artifact is embedded in the shell, so there is exactly
  one frontend build.
- Remote desktop support can access the web UI directly (no Tauri needed).
- Auto-update (ADR-0006) ships the whole package as one signed artifact — the
  Tauri bundle carries the frozen PyInstaller backend alongside the shell via
  `bundle.resources` (see `docs/dev/packaging.md`, the K6 installer-size gate);
  the transport posture of the admin panel (loopback default, optional TLS) is
  ADR-0007.
