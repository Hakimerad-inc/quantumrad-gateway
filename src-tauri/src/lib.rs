use std::sync::atomic::{AtomicU8, Ordering};
use std::sync::Arc;
use std::time::Duration;

use tauri::{
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    Manager, WindowEvent,
};
use tauri_plugin_shell::ShellExt;

// Tray states matching the Python derive_tray_state() output.
const TRAY_IDLE: u8 = 0;
const TRAY_SENDING: u8 = 1;
const TRAY_ERROR: u8 = 2;

/// The port the packaged Python sidecar binds (must match
/// `config.web_ui.port` default and the SPA's `TAURI_API_BASE`).
const BACKEND_PORT: u16 = 8080;

/// Backend polls must never hang the tray thread: 2 s to connect + read.
/// `reqwest::blocking` without a timeout can block the poll loop forever on
/// a wedged socket (review M10).
const HTTP_TIMEOUT: Duration = Duration::from_secs(2);

/// Decode an embedded PNG into a Tauri Image.
fn load_icon(bytes: &'static [u8]) -> Result<tauri::image::Image<'static>, Box<dyn std::error::Error>> {
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
fn backend_candidates() -> Vec<std::path::PathBuf> {
    let exe_name = if cfg!(windows) {
        "mercure-gateway.exe"
    } else {
        "mercure-gateway"
    };
    let rel = std::path::Path::new("binaries").join("mercure-gateway").join(exe_name);
    let mut candidates = Vec::new();
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            // (a) resource dir == exe dir (Windows NSIS, deb/usr/bin)
            candidates.push(dir.join(&rel));
            // (b) deb layout: /usr/lib/<app>/binaries/... next to /usr/bin exe
            if let Some(parent) = dir.parent() {
                candidates.push(parent.join("lib").join(&rel));
                // (c) AppImage / macOS layout: ../../Resources or ../.. mapping
                candidates.push(parent.join(&rel));
            }
        }
    }
    // (d) dev fallback: run from src-tauri (cargo tauri dev) — resources stay
    // in the manifest dir.
    if let Ok(manifest_dir) = std::env::var("CARGO_MANIFEST_DIR") {
        candidates.push(
            std::path::PathBuf::from(manifest_dir).join(&rel),
        );
    }
    candidates
}

fn find_backend() -> Option<std::path::PathBuf> {
    backend_candidates().into_iter().find(|p| p.exists())
}

/// GET *path* on the backend with the shared timeout. `Ok(None)` = transport
/// failure (offline / wedged backend) — callers treat it as an unknown value.
fn backend_get_json(path: &str) -> Result<Option<serde_json::Value>, Box<dyn std::error::Error>> {
    let url = format!("http://127.0.0.1:{BACKEND_PORT}{path}");
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
fn derive_state(status: &serde_json::Value) -> u8 {
    let receiver = status["receiver"].as_str().unwrap_or("stopped");
    let forwarder = status["forwarder"].as_str().unwrap_or("stopped");
    let retriever = status["report_retriever"].as_str().unwrap_or("stopped");

    if receiver != "running" || forwarder != "running" || retriever != "running" {
        return TRAY_ERROR;
    }

    if let Ok(Some(data)) = backend_get_json("/api/queue/stats") {
        let sending = data["sending"].as_u64().unwrap_or(0);
        let queued = data["queued"].as_u64().unwrap_or(0);
        let error = data["error"].as_u64().unwrap_or(0);
        let failed = data["failed"].as_u64().unwrap_or(0);
        if error > 0 || failed > 0 {
            return TRAY_ERROR;
        }
        if sending > 0 || queued > 0 {
            return TRAY_SENDING;
        }
    }

    TRAY_IDLE
}

fn state_label(state: u8) -> &'static str {
    match state {
        TRAY_SENDING => "QuantumRAD Gateway — sending",
        TRAY_ERROR => "QuantumRAD Gateway — attention needed",
        _ => "QuantumRAD Gateway — idle",
    }
}

/// Apply the tray visuals for a state: a distinct icon glyph per state
/// (green ring = idle, amber ring + dot = sending, red ring + exclamation =
/// error) plus a tooltip naming the state. Previously the same icon was
/// applied for every state, so the operator had no at-a-glance status without
/// opening the window (review M10).
fn apply_tray_state(
    tray: &tauri::tray::TrayIcon,
    icons: &TrayIcons,
    state: u8,
) -> Result<(), Box<dyn std::error::Error>> {
    let icon = match state {
        TRAY_SENDING => &icons.sending,
        TRAY_ERROR => &icons.error,
        _ => &icons.idle,
    };
    tray.set_icon(Some(icon.clone()))?;
    tray.set_tooltip(Some(state_label(state)))?;
    Ok(())
}

/// The three bundled state glyphs, decoded once at startup.
struct TrayIcons {
    idle: tauri::image::Image<'static>,
    sending: tauri::image::Image<'static>,
    error: tauri::image::Image<'static>,
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let state = Arc::new(AtomicU8::new(TRAY_IDLE));
    let state_clone = state.clone();

    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        // Auto-update plumbing (ADR-0006): the updater verifies Ed25519-signed
        // artifacts against the pubkey in tauri.conf.json (overridden at
        // release time via tauri.release.conf.json); the process plugin
        // performs the relaunch after an update is installed.
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_process::init())
        .setup(move |app| {
            // ── Tray icon ──────────────────────────────────────────
            let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&quit])?;
            let icons = TrayIcons {
                idle: load_icon(include_bytes!("../icons/tray-idle.png"))?,
                sending: load_icon(include_bytes!("../icons/tray-sending.png"))?,
                error: load_icon(include_bytes!("../icons/tray-error.png"))?,
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
            match find_backend() {
                Some(path) => match app.shell().command(path).args(["--web"]).spawn() {
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
                    Err(e) => eprintln!("mercure-gateway backend failed to start: {e}"),
                },
                None => eprintln!(
                    "mercure-gateway backend bundle not found (run scripts/package_backend.py)"
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
                        Ok(Some(status)) => derive_state(&status),
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
        let candidates = backend_candidates();
        // The exe-dir candidate must always be first when we can resolve one.
        if let Ok(exe) = std::env::current_exe() {
            let dir = exe.parent().unwrap();
            let expected = dir
                .join("binaries")
                .join("mercure-gateway")
                .join(if cfg!(windows) { "mercure-gateway.exe" } else { "mercure-gateway" });
            assert_eq!(candidates[0], expected);
        }
    }

    #[test]
    fn find_backend_returns_none_when_bundle_absent() {
        // In the dev/test environment the frozen bundle may legitimately not
        // exist — find_backend must return None rather than a bogus path.
        let candidates = backend_candidates();
        let any_real = candidates.iter().any(|p| p.exists());
        assert_eq!(find_backend().is_some(), any_real);
    }
}
