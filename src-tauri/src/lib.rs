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
        TRAY_SENDING => "mercure-gateway — sending",
        TRAY_ERROR => "mercure-gateway — attention needed",
        _ => "mercure-gateway — idle",
    }
}

/// Apply the tray visuals for a state. Distinct state glyphs are still a
/// polish item (one bundled icon), but the tooltip now reflects the polled
/// state — previously the computed `TRAY_*` value was stored and never read
/// anywhere, so the operator could not see status without opening the window
/// (review M10).
fn apply_tray_state(
    tray: &tauri::tray::TrayIcon,
    icon: &tauri::image::Image<'static>,
    state: u8,
) -> Result<(), Box<dyn std::error::Error>> {
    tray.set_icon(Some(icon.clone()))?;
    tray.set_tooltip(Some(state_label(state)))?;
    Ok(())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let state = Arc::new(AtomicU8::new(TRAY_IDLE));
    let state_clone = state.clone();

    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .setup(move |app| {
            // ── Tray icon ──────────────────────────────────────────
            let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&quit])?;
            let icon = load_icon(include_bytes!("../icons/128x128.png"))?;
            let icon_handle = Arc::new(icon);

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
            // Launch the Python backend so the web admin API is reachable on
            // 127.0.0.1:8080 (the SPA, loaded from the tauri://localhost
            // origin, calls it there). The binary is a packaging artifact: the
            // build pipeline must freeze the backend (e.g. via PyInstaller) into
            // `src-tauri/binaries/mercure-gateway-<target-triple>` and declare it
            // in tauri.conf.json `bundle.externalBin`. Until then this spawn
            // fails gracefully (logged below) and the app still launches — it
            // just has no backend to talk to.
            match app.shell().sidecar("mercure-gateway") {
                Ok(cmd) => match cmd.args(["--web"]).spawn() {
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
                    Err(e) => eprintln!("mercure-gateway sidecar failed to start: {e}"),
                },
                Err(e) => eprintln!("mercure-gateway sidecar not configured: {e}"),
            }

            // ── Background poller ──────────────────────────────────
            let tray_handle = tray.clone();
            let poll_icon = icon_handle.clone();
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
                    let _ = apply_tray_state(&tray_handle, &poll_icon, new_state);
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
