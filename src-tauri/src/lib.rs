use std::sync::atomic::{AtomicU8, Ordering};
use std::sync::Arc;
use std::time::Duration;

use tauri::{
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    Manager, WindowEvent,
};

// Tray states matching the Python derive_tray_state() output.
const TRAY_IDLE: u8 = 0;
const TRAY_SENDING: u8 = 1;
const TRAY_ERROR: u8 = 2;

/// Decode an embedded PNG into a Tauri Image.
fn load_icon(bytes: &'static [u8]) -> Result<tauri::image::Image<'static>, Box<dyn std::error::Error>> {
    let decoder = png::Decoder::new(std::io::Cursor::new(bytes));
    let mut reader = decoder.read_info()?;
    let mut buf = vec![0u8; reader.output_buffer_size()];
    let info = reader.next_frame(&mut buf)?;
    let (width, height) = (info.width, info.height);
    Ok(tauri::image::Image::new_owned(buf, width, height))
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

    let queue = reqwest::blocking::get("http://127.0.0.1:8080/api/queue/stats");
    if let Ok(r) = queue {
        if let Ok(data) = r.json::<serde_json::Value>() {
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
    }

    TRAY_IDLE
}

/// Set the tray icon based on the numeric state (idle/sending/error share the
/// bundled icon for now; distinct state glyphs are a S09 polish item).
fn set_tray_icon(
    tray: &tauri::tray::TrayIcon,
    icon: &tauri::image::Image<'static>,
) -> Result<(), Box<dyn std::error::Error>> {
    tray.set_icon(Some(icon.clone()))?;
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

            // ── Background poller ──────────────────────────────────
            let tray_handle = tray.clone();
            let poll_icon = icon_handle.clone();
            std::thread::spawn(move || {
                loop {
                    std::thread::sleep(Duration::from_secs(5));
                    if let Ok(resp) =
                        reqwest::blocking::get("http://127.0.0.1:8080/api/system/status")
                    {
                        if let Ok(status) = resp.json::<serde_json::Value>() {
                            let new_state = derive_state(&status);
                            state_clone.store(new_state, Ordering::Relaxed);
                            let _ = set_tray_icon(&tray_handle, &poll_icon);
                        }
                    }
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
