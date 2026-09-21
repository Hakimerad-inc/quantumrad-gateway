use std::sync::Arc;
use std::sync::atomic::{AtomicU8, Ordering};
use std::time::Duration;

use tauri::{
    Manager, WindowEvent,
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
};
use tauri_plugin_shell::ShellExt;

mod sidecar;

// Tray states matching the Python derive_tray_state() output
// (src/mercure_gateway/tray.py). Priority: error > sending > removable > idle.
const TRAY_IDLE: u8 = 0;
const TRAY_SENDING: u8 = 1;
const TRAY_ERROR: u8 = 2;
/// USB variant is active and the gateway is otherwise idle — the spool lives on
/// removable media and it is safe to unplug (PRD §2.2 Flow C).
const TRAY_REMOVABLE: u8 = 3;

/// Default assumed for a MISSING component field in the status payload.
///
/// DELIBERATE DIVERGENCE from Python: `tray.py` does
/// `kwargs.get("receiver", "running")` and would read a missing field as
/// healthy; we read it as stopped, so a status payload that drops a component
/// key yields TRAY_ERROR ("attention needed") rather than a green idle tray.
/// Both are defensible — Python is the spec, not a collaborator — but on a
/// medical appliance the tray is the operator's only at-a-glance signal, and a
/// schema change silently rendering it "all clear" is worse than a transient
/// false alarm. Rust fails closed; the live endpoint serializes all three
/// fields explicitly (web/routes.py `SystemStatus`), so this only ever fires
/// on a genuine contract change, which is exactly when we want the tray
/// shouting. Pinned by `derive_state_missing_component_fields_is_error`.
const COMPONENT_DEFAULT: &str = "stopped";

/// The port the packaged Python sidecar binds (must match
/// `config.web_ui.port` default and the SPA's `TAURI_API_BASE`).
/// Overridable via `MERCURE_BACKEND_PORT` so a packaged install can coexist
/// with a dev instance or another service on 8080 (release-day rehearsal
/// 2026-09-14 hit exactly this: openpacs holding 8080 blocked the tray demo).
const BACKEND_PORT: u16 = 8080;

/// Effective backend port: `MERCURE_BACKEND_PORT` when it parses, else 8080.
fn backend_port() -> u16 {
    backend_port_from(std::env::var("MERCURE_BACKEND_PORT").ok())
}

/// Pure half of [`backend_port`] — a bad value (garbage, out of range) falls
/// back to the default rather than killing the tray.
fn backend_port_from(raw: Option<String>) -> u16 {
    raw.and_then(|v| v.trim().parse::<u16>().ok())
        .filter(|p| *p > 0)
        .unwrap_or(BACKEND_PORT)
}

/// Backend polls must never hang the tray thread: 2 s to connect + read.
/// `reqwest::blocking` without a timeout can block the poll loop forever on
/// a wedged socket (review M10).
const HTTP_TIMEOUT: Duration = Duration::from_secs(2);

/// Decode an embedded PNG into a Tauri Image.
fn load_icon(
    bytes: &'static [u8],
) -> Result<tauri::image::Image<'static>, Box<dyn std::error::Error>> {
    let decoder = png::Decoder::new(std::io::Cursor::new(bytes));
    let mut reader = decoder.read_info()?;
    let mut buf = vec![0u8; reader.output_buffer_size()];
    let info = reader.next_frame(&mut buf)?;
    let (width, height) = (info.width, info.height);
    Ok(tauri::image::Image::new_owned(buf, width, height))
}

/// Candidate locations of the frozen Python backend (PyInstaller onedir,
/// bundled via `bundle.resources`, see docs/dev/packaging.md). Tauri's
/// `externalBin` only supports single files, so the onedir directory rides in
/// resources and we spawn it by explicit path. First existing candidate wins;
/// an empty result (or failed spawn) is non-fatal — the app launches without
/// a backend, exactly like the previous `sidecar()` failure path.
fn backend_candidates(product: &str) -> Vec<std::path::PathBuf> {
    let exe_name = if cfg!(windows) {
        "mercure-gateway.exe"
    } else {
        "mercure-gateway"
    };
    let rel = std::path::Path::new("binaries")
        .join("mercure-gateway")
        .join(exe_name);
    let mut candidates = Vec::new();
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            // (a) resource dir == exe dir (Windows NSIS, dev cargo run)
            candidates.push(dir.join(&rel));
            // (b) deb layout: /usr/lib/<productName>/binaries/... with the
            // shell at /usr/bin/<name>. The dir under /usr/lib is the
            // productName, not the binary name — resolved candidates from the
            // 2026-09-14 tray rehearsal (sidecar was never found without it).
            if let Some(parent) = dir.parent() {
                candidates.push(parent.join("lib").join(product).join(&rel));
                // (c) AppImage / macOS layout: ../../Resources or ../.. mapping
                candidates.push(parent.join(&rel));
            }
        }
    }
    // (d) dev fallback: run from src-tauri (cargo tauri dev) — resources stay
    // in the manifest dir.
    if let Ok(manifest_dir) = std::env::var("CARGO_MANIFEST_DIR") {
        candidates.push(std::path::PathBuf::from(manifest_dir).join(&rel));
    }
    candidates
}

fn find_backend(product: &str) -> Option<std::path::PathBuf> {
    backend_candidates(product).into_iter().find(|p| p.exists())
}

/// GET *path* on the backend with the shared timeout. `Ok(None)` = transport
/// failure (offline / wedged backend) — callers treat it as an unknown value.
fn backend_get_json(path: &str) -> Result<Option<serde_json::Value>, Box<dyn std::error::Error>> {
    let url = format!("http://127.0.0.1:{}{path}", backend_port());
    let resp = reqwest::blocking::Client::builder()
        .timeout(HTTP_TIMEOUT)
        .build()?
        .get(&url)
        .send()?;
    if !resp.status().is_success() {
        return Ok(None);
    }
    Ok(resp.json::<serde_json::Value>().ok())
}

/// Poll the FastAPI status endpoint and map the result to a tray state.
/// The Rust side mirrors the Python `derive_tray_state` logic so the shell
/// can update the icon without a round-trip through the Python process.
///
/// Split into a pure half ([`derive_state`], both inputs passed in — the only
/// way the queue branch can be reached from a unit test) and this thin I/O
/// caller that owns the second fetch. The queue stats are fetched *after* the
/// status check so a wedged backend short-circuits to error before we spend a
/// second request on a queue we cannot act on.
fn derive_state_live(status: &serde_json::Value) -> u8 {
    let queue_stats = backend_get_json("/api/queue/stats");
    derive_state(status, queue_stats.ok().flatten().as_ref())
}

/// Pure half of the tray state machine: map the status + queue stats payloads
/// (already fetched) to a tray state. Mirrors `derive_tray_state` priority —
/// error > sending > removable > idle — and is the unit-testable core.
///
/// `queue_stats` is `None` when the `/api/queue/stats` request failed at the
/// transport level. A failed queue fetch does NOT promote to error (the status
/// endpoint already proved the backend is up and every component running, so a
/// flaky second request degrading to "idle" matches the pre-split behaviour
/// and avoids crying wolf on a transient) — but it DOES suppress removable: a
/// safe-to-remove hint is an invitation to unplug, and without queue data we
/// have no evidence the spool is quiet. No evidence, no invitation (PRD §2.2
/// Flow C).
///
/// `usb_mode` is read from the same status object the poll loop already holds
/// rather than a third request or a boot-time snapshot — main.py can flip
/// `usb_mode.enabled` at runtime, so a snapshot goes stale.
fn derive_state(status: &serde_json::Value, queue_stats: Option<&serde_json::Value>) -> u8 {
    let receiver = status["receiver"].as_str().unwrap_or(COMPONENT_DEFAULT);
    let forwarder = status["forwarder"].as_str().unwrap_or(COMPONENT_DEFAULT);
    let retriever = status["report_retriever"]
        .as_str()
        .unwrap_or(COMPONENT_DEFAULT);

    if receiver != "running" || forwarder != "running" || retriever != "running" {
        return TRAY_ERROR;
    }

    match queue_stats {
        Some(data) => {
            let sending = data["sending"].as_u64().unwrap_or(0);
            let queued = data["queued"].as_u64().unwrap_or(0);
            let error = data["error"].as_u64().unwrap_or(0);
            let failed = data["failed"].as_u64().unwrap_or(0);
            if error > 0 || failed > 0 {
                return TRAY_ERROR;
            }
            // Removable is only reachable with a provably empty queue: a
            // safe-to-remove hint shown while studies are mid-transfer is
            // actively dangerous on a USB appliance, so the sending branch
            // must come first.
            if sending > 0 || queued > 0 {
                return TRAY_SENDING;
            }
        }
        // Queue data unavailable: fall to idle, not removable (see above).
        None => return TRAY_IDLE,
    }

    if status["usb_mode"].as_bool().unwrap_or(false) {
        return TRAY_REMOVABLE;
    }

    TRAY_IDLE
}

fn state_label(state: u8) -> &'static str {
    match state {
        TRAY_IDLE => "QuantumRAD Gateway — idle",
        TRAY_SENDING => "QuantumRAD Gateway — sending",
        TRAY_ERROR => "QuantumRAD Gateway — attention needed",
        TRAY_REMOVABLE => "QuantumRAD Gateway — safe to remove",
        // The four states are all u8 constants; this arm is unreachable for
        // any value this codebase sets. Kept (rather than an unreachable!()) so
        // a future fifth state added without a label arm degrades to the
        // neutral idle string instead of panicking the poll thread.
        _ => "QuantumRAD Gateway",
    }
}

/// Apply the tray visuals for a state: a distinct icon glyph per state
/// (green ring = idle, amber ring + dot = sending, red ring + exclamation =
/// error, blue ring + check = safe to remove) plus a tooltip naming the state.
/// Previously the same icon was applied for every state, so the operator had
/// no at-a-glance status without opening the window (review M10).
fn apply_tray_state(
    tray: &tauri::tray::TrayIcon,
    icons: &TrayIcons,
    state: u8,
) -> Result<(), Box<dyn std::error::Error>> {
    let icon = match state {
        TRAY_IDLE => &icons.idle,
        TRAY_SENDING => &icons.sending,
        TRAY_ERROR => &icons.error,
        TRAY_REMOVABLE => &icons.removable,
        // Same reasoning as state_label: never panic the poll thread.
        _ => &icons.idle,
    };
    tray.set_icon(Some(icon.clone()))?;
    tray.set_tooltip(Some(state_label(state)))?;
    Ok(())
}

/// The bundled state glyphs, decoded once at startup.
struct TrayIcons {
    idle: tauri::image::Image<'static>,
    sending: tauri::image::Image<'static>,
    error: tauri::image::Image<'static>,
    removable: tauri::image::Image<'static>,
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let state = Arc::new(AtomicU8::new(TRAY_IDLE));
    let state_clone = state.clone();
    // Injected before the SPA loads so apiUrl() targets the same port the
    // sidecar was told to bind (MERCURE_BACKEND_PORT override, e.g. to
    // coexist with another service on 8080).
    let boot_port = backend_port();

    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        // Structured logging (review: the `log` crate was declared but every
        // diagnostic was an eprintln! to a console no operator watches). The
        // plugin installs a global logger before setup runs, so the sidecar
        // and poll-loop diagnostics below reach both stdout and the
        // platform's app log dir — the file an operator actually attaches to
        // a support ticket. Level is Info so the periodic poll-loop noise
        // stays out unless something is actually wrong (warnings/errors).
        .plugin(
            tauri_plugin_log::Builder::new()
                .targets([
                    tauri_plugin_log::Target::new(tauri_plugin_log::TargetKind::LogDir {
                        file_name: None,
                    }),
                    tauri_plugin_log::Target::new(tauri_plugin_log::TargetKind::Stdout),
                ])
                .level(log::LevelFilter::Info)
                .build(),
        )
        // Auto-update plumbing (ADR-0006): the updater verifies Ed25519-signed
        // artifacts against the pubkey in tauri.conf.json (overridden at
        // release time via tauri.release.conf.json); the process plugin
        // performs the relaunch after an update is installed.
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_process::init())
        .setup(move |app| {
            // ── Main window ────────────────────────────────────────
            // Built here (not from tauri.conf.json) so the init script that
            // publishes __MERCURE_PORT__ to the SPA can be attached — the
            // builder API exposes initialization_script, the config schema
            // does not. Mirrors the former config window (1280x800, titled);
            // hidden here and shown at the end of setup as before.
            tauri::WebviewWindowBuilder::new(
                app,
                "main",
                tauri::WebviewUrl::App("index.html".into()),
            )
            .title("QuantumRAD Gateway")
            .inner_size(1280.0, 800.0)
            .resizable(true)
            .fullscreen(false)
            .visible(false)
            .initialization_script(format!("window.__MERCURE_PORT__ = {boot_port};"))
            .build()?;

            // ── Tray icon ──────────────────────────────────────────
            let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&quit])?;
            let icons = TrayIcons {
                idle: load_icon(include_bytes!("../icons/tray-idle.png"))?,
                sending: load_icon(include_bytes!("../icons/tray-sending.png"))?,
                error: load_icon(include_bytes!("../icons/tray-error.png"))?,
                removable: load_icon(include_bytes!("../icons/tray-removable.png"))?,
            };
            let icons = Arc::new(icons);
            let icon_handle = Arc::new(icons.idle.clone());

            let tray = TrayIconBuilder::new()
                .icon(icon_handle.as_ref().clone())
                .menu(&menu)
                .on_menu_event(|app, event| {
                    if event.id.as_ref() == "quit" {
                        app.exit(0);
                    }
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        if let Some(window) = tray.app_handle().get_webview_window("main") {
                            let _ = window.show();
                            let _ = window.set_focus();
                        }
                    }
                })
                .build(app)?;

            // ── Backend sidecar (review C2) ──────────────────────
            // Launch the frozen Python backend so the web admin API is
            // reachable on 127.0.0.1:8080 (the SPA, loaded from the
            // tauri://localhost origin, calls it there). The backend is a
            // PyInstaller onedir bundle produced by scripts/package_backend.py
            // and shipped via `bundle.resources`; `externalBin` cannot carry a
            // directory (tauri-apps/tauri#6676), hence the explicit-path
            // spawn. A missing bundle or failed spawn is non-fatal (logged
            // below) — the app launches, just without a backend.
            let product = app
                .config()
                .product_name
                .clone()
                .unwrap_or_else(|| "QuantumRAD-Gateway".into());
            match find_backend(&product) {
                Some(path) => {
                    let port_arg = boot_port.to_string();
                    match app
                        .shell()
                        .command(path)
                        .args(["--web", "--port", &port_arg])
                        .spawn()
                    {
                        Ok((mut rx, child)) => {
                            // Keep the child alive for the app's lifetime and drain
                            // its stdout/stderr so the OS pipes never fill and block
                            // it. The async task ends when the child exits and the
                            // channel closes.
                            tauri::async_runtime::spawn(async move {
                                let _child = child;
                                while let Some(_event) = rx.recv().await {}
                            });
                        }
                        Err(e) => log::error!("backend failed to start: {e}"),
                    }
                }
                None => log::warn!(
                    "backend bundle not found (run scripts/package_backend.py); \
                     launching without a backend"
                ),
            }

            // ── Background poller ──────────────────────────────────
            let tray_handle = tray.clone();
            let poll_icons = icons.clone();
            std::thread::spawn(move || {
                loop {
                    std::thread::sleep(Duration::from_secs(5));
                    // Transport failure = backend unreachable: that is exactly
                    // the "attention needed" condition, so map it to TRAY_ERROR
                    // instead of silently skipping the update (review M10).
                    let new_state = match backend_get_json("/api/system/status") {
                        Ok(Some(status)) => derive_state_live(&status),
                        Ok(None) | Err(_) => TRAY_ERROR,
                    };
                    state_clone.store(new_state, Ordering::Relaxed);
                    let _ = apply_tray_state(&tray_handle, &poll_icons, new_state);
                }
            });

            // ── Show the main window ───────────────────────────────
            let _ = app.get_webview_window("main").map(|w| w.show());
            Ok(())
        })
        .on_window_event(|window, event| {
            // Hide instead of close on window close (keep tray alive)
            if let WindowEvent::CloseRequested { api, .. } = event {
                let _ = window.hide();
                api.prevent_close();
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn backend_candidates_orders_exe_dir_first() {
        let candidates = backend_candidates("QuantumRAD-Gateway");
        // The exe-dir candidate must always be first when we can resolve one.
        if let Ok(exe) = std::env::current_exe() {
            let dir = exe.parent().unwrap();
            let expected = dir
                .join("binaries")
                .join("mercure-gateway")
                .join(if cfg!(windows) {
                    "mercure-gateway.exe"
                } else {
                    "mercure-gateway"
                });
            assert_eq!(candidates[0], expected);
        }
    }

    #[test]
    fn backend_candidates_deb_layout_uses_product_name() {
        // Regression proven by the 2026-09-14 tray rehearsal: the deb installs
        // the shell at /usr/bin/mercure-gateway and the sidecar at
        // /usr/lib/<productName>/binaries/... — candidate (b) must carry the
        // productName, not the exe dir name.
        let candidates = backend_candidates("QuantumRAD-Gateway");
        let deb = candidates.iter().find(|p| {
            p.components()
                .any(|c| c.as_os_str() == "QuantumRAD-Gateway")
        });
        assert!(
            deb.is_some(),
            "deb-layout candidate missing productName dir"
        );
        if let Some(p) = deb {
            let s = p.display().to_string();
            assert!(
                s.contains("lib/QuantumRAD-Gateway/binaries/mercure-gateway"),
                "{s}"
            );
        }
    }

    #[test]
    fn find_backend_returns_none_when_bundle_absent() {
        // In the dev/test environment the frozen bundle may legitimately not
        // exist — find_backend must return None rather than a bogus path.
        let candidates = backend_candidates("QuantumRAD-Gateway");
        let any_real = candidates.iter().any(|p| p.exists());
        assert_eq!(find_backend("QuantumRAD-Gateway").is_some(), any_real);
    }

    #[test]
    fn backend_port_override_parses_and_falls_back() {
        assert_eq!(backend_port_from(Some("18080".into())), 18080);
        assert_eq!(backend_port_from(Some(" 8090 ".into())), 8090);
        // Garbage / out-of-range / zero all fall back to the 8080 default
        // rather than panicking or binding a dead port.
        assert_eq!(backend_port_from(Some("http://x".into())), BACKEND_PORT);
        assert_eq!(backend_port_from(Some("70000".into())), BACKEND_PORT);
        assert_eq!(backend_port_from(Some("0".into())), BACKEND_PORT);
        assert_eq!(backend_port_from(None), BACKEND_PORT);
    }

    /// Shape of `/api/system/status` as the poll loop receives it. `usb_mode`
    /// rides the same object so no third request or boot snapshot is needed.
    fn status_json(usb_mode: bool) -> serde_json::Value {
        serde_json::json!({
            "receiver": "running",
            "forwarder": "running",
            "report_retriever": "running",
            "usb_mode": usb_mode,
        })
    }

    fn queue_json(sending: u64, queued: u64, error: u64, failed: u64) -> serde_json::Value {
        serde_json::json!({ "sending": sending, "queued": queued, "error": error, "failed": failed })
    }

    #[test]
    fn derive_state_maps_all_four_states() {
        let empty = queue_json(0, 0, 0, 0);
        // components running + empty queue + no usb → idle
        assert_eq!(derive_state(&status_json(false), Some(&empty)), TRAY_IDLE);
        // components running + empty queue + usb → removable
        assert_eq!(
            derive_state(&status_json(true), Some(&empty)),
            TRAY_REMOVABLE
        );
        // active work → sending
        assert_eq!(
            derive_state(&status_json(true), Some(&queue_json(1, 0, 0, 0))),
            TRAY_SENDING
        );
        assert_eq!(
            derive_state(&status_json(false), Some(&queue_json(0, 3, 0, 0))),
            TRAY_SENDING
        );
        // failed/error studies → error
        assert_eq!(
            derive_state(&status_json(true), Some(&queue_json(0, 0, 2, 0))),
            TRAY_ERROR
        );
        assert_eq!(
            derive_state(&status_json(true), Some(&queue_json(0, 0, 0, 1))),
            TRAY_ERROR
        );
    }

    #[test]
    fn derive_state_priority_is_error_over_sending_over_removable() {
        // usb_mode is set throughout — removable is the *lowest* of these three
        // and must never win when anything higher is true.
        let status = status_json(true);
        // error beats sending: failed work + active work → error
        assert_eq!(
            derive_state(&status, Some(&queue_json(5, 5, 0, 1))),
            TRAY_ERROR
        );
        // error beats removable: failed work, nothing active → error
        assert_eq!(
            derive_state(&status, Some(&queue_json(0, 0, 1, 0))),
            TRAY_ERROR
        );
        // sending beats removable: active work, nothing failed → sending
        assert_eq!(
            derive_state(&status, Some(&queue_json(1, 1, 0, 0))),
            TRAY_SENDING
        );
        // nothing pending → removable, proving the arm exists and is last
        assert_eq!(
            derive_state(&status, Some(&queue_json(0, 0, 0, 0))),
            TRAY_REMOVABLE
        );
    }

    #[test]
    fn derive_state_usb_mode_with_busy_queue_is_never_removable() {
        // The dangerous case on a USB appliance: an operator unplugging mid-
        // transfer. Any non-empty queue must suppress the hint even when the
        // components are healthy and usb_mode is on.
        for (sending, queued, error, failed) in [
            (1, 0, 0, 0),
            (0, 1, 0, 0),
            (3, 3, 0, 0),
            (0, 0, 1, 0),
            (0, 0, 0, 7),
        ] {
            let got = derive_state(
                &status_json(true),
                Some(&queue_json(sending, queued, error, failed)),
            );
            assert_ne!(
                got, TRAY_REMOVABLE,
                "usb_mode + queue(sending={sending}, queued={queued}, error={error}, failed={failed}) must not be removable"
            );
        }
    }

    #[test]
    fn derive_state_stopped_component_is_error_even_with_usb_mode() {
        // usb_mode never masks a broken component — removable is only
        // reachable when every component reports running.
        for field in ["receiver", "forwarder", "report_retriever"] {
            let mut status = status_json(true);
            status[field] = serde_json::json!("stopped");
            assert_eq!(
                derive_state(&status, Some(&queue_json(0, 0, 0, 0))),
                TRAY_ERROR,
                "{field} stopped with usb_mode must be error"
            );
        }
        // Same for transient non-running states the endpoint may emit.
        let mut status = status_json(true);
        status["forwarder"] = serde_json::json!("starting");
        assert_eq!(
            derive_state(&status, Some(&queue_json(0, 0, 0, 0))),
            TRAY_ERROR
        );
    }

    #[test]
    fn derive_state_missing_component_fields_is_error() {
        // DIVERGENT DEFAULTS, pinned: Python (tray.py:32-34) treats a missing
        // component field as "running"; Rust treats it as COMPONENT_DEFAULT
        // ("stopped") and therefore TRAY_ERROR. The live endpoint always
        // serializes all three fields, so this only fires on a schema change —
        // exactly when the tray should fail closed rather than show green.
        let partial = serde_json::json!({"usb_mode": true});
        assert_eq!(
            derive_state(&partial, Some(&queue_json(0, 0, 0, 0))),
            TRAY_ERROR,
            "missing component fields must not read as a healthy gateway"
        );
        // A single missing field is enough; the other two being present does
        // not rescue it.
        let mut partial_one = status_json(true);
        partial_one["forwarder"].take();
        assert_eq!(
            derive_state(&partial_one, Some(&queue_json(0, 0, 0, 0))),
            TRAY_ERROR
        );
    }

    #[test]
    fn derive_state_unavailable_queue_suppresses_removable() {
        // The queue fetch failed at the transport level. The backend is up
        // (status came back), so this is not an error — but without queue data
        // we have no proof the spool is quiet, so the safe-to-remove hint must
        // not light up.
        assert_eq!(derive_state(&status_json(true), None), TRAY_IDLE);
        assert_eq!(derive_state(&status_json(false), None), TRAY_IDLE);
        // A stopped component still wins over the unavailable queue.
        let mut broken = status_json(true);
        broken["receiver"] = serde_json::json!("stopped");
        assert_eq!(derive_state(&broken, None), TRAY_ERROR);
    }

    #[test]
    fn derive_state_missing_usb_mode_is_not_removable() {
        // Older backend without the B10 field: default false, never a
        // safe-to-remove hint we are not entitled to claim.
        let no_field = serde_json::json!({
            "receiver": "running",
            "forwarder": "running",
            "report_retriever": "running",
        });
        assert_eq!(
            derive_state(&no_field, Some(&queue_json(0, 0, 0, 0))),
            TRAY_IDLE
        );
    }

    #[test]
    fn state_label_names_all_four_states() {
        // The label is the tooltip an operator reads; each state needs its own
        // text (the `_` arm used to silently flatten removable to idle).
        assert_eq!(state_label(TRAY_IDLE), "QuantumRAD Gateway — idle");
        assert_eq!(state_label(TRAY_SENDING), "QuantumRAD Gateway — sending");
        assert_eq!(
            state_label(TRAY_ERROR),
            "QuantumRAD Gateway — attention needed"
        );
        assert_eq!(
            state_label(TRAY_REMOVABLE),
            "QuantumRAD Gateway — safe to remove"
        );
        // The fallback arm stays neutral rather than claiming a state.
        assert_eq!(state_label(255), "QuantumRAD Gateway");
    }
}
