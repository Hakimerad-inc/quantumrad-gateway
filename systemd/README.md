# Headless systemd deployment runbook (TD-09)

This runbook installs the gateway as a **systemd user service** for headless
deployments (a small Linux box / VM running the store-and-forward node without
a desktop session for the window).

The unit (`mercure-gateway.service`) provides:

-   **Supervision** — `Restart=always` with a 10s backoff; crashes self-heal.
-   **Health check** — `ExecStartPost` probes the web admin API after boot and
    only lets the service reach `running` once the gateway serves requests.
-   **Hardening** — a set of systemd security directives that do **not**
    break USB-dongle, keyring, or spool behavior (see §Hardening).
-   **Secrets via EnvironmentFile** — port, config path and `MERCURE_GATEWAY_*`
    / `MERCURE_MASTER_PASSWORD*` secrets come from `gateway.env`, never the
    unit file or (necessarily) the config JSON (TD-08).

## 1. Prerequisites

-   `uv` installed for the service user (standalone installer puts it at
    `~/.local/bin/uv`; `cargo install uv` puts it at `~/.cargo/bin/uv`).
    Verify: `uv --version`.
-   `curl` available for the health check.
-   systemd ≥ 240 (user units).

## 2. Install

```bash
# Working directory for the service (uv needs the project context).
mkdir -p ~/mercure-gateway             # repo checkout (or symlink to it)
# If the gateway is installed as a tool instead — `uv tool install .` — point
# ExecStart at ~/.local/bin/mercure-gateway and WorkingDirectory is optional.

# Unit + env file.
mkdir -p ~/.config/systemd/user ~/.config/mercure-gateway
cp systemd/mercure-gateway.service ~/.config/systemd/user/
cp systemd/gateway.env.example ~/.config/mercure-gateway/gateway.env

# Edit the env file: at minimum set MERCURE_GATEWAY_CONFIG.
vim ~/.config/mercure-gateway/gateway.env
chmod 600 ~/.config/mercure-gateway/gateway.env

systemctl --user daemon-reload
systemctl --user enable --now mercure-gateway
```

The gateway boots with:

    uv run mercure-gateway --config <MERCURE_GATEWAY_CONFIG> --web --port <MERCURE_GATEWAY_PORT>

## 3. Ops / day-2

```bash
systemctl --user status mercure-gateway     # running / exited with failure
journalctl --user -u mercure-gateway -f     # live logs
journalctl --user -u mercure-gateway -S today
systemctl --user restart mercure-gateway    # reload config / rotate secrets
systemctl --user disable --now mercure-gateway   # uninstall
```

Secrets are rotated by editing `gateway.env` and restarting — the old value
never appears on disk in the unit or, if `credentials.encrypted=true`, in the
config JSON (see `docs/guides/secrets-and-env-overrides.md`).

## 4. Health check

On boot, the unit retries for 30 seconds:

    curl -fs http://127.0.0.1:${MERCURE_GATEWAY_PORT}/api/system/health

With the default `web_ui.auth_enabled=false` (loopback-only admin) this returns
`200 {"status":"ok",...}`. If you enable web authentication, `/api/system/health`
answers `401` and the probe must target the auth-free SPA root instead — edit
the unit's `ExecStartPost` line to:

    ExecStartPost=/bin/sh -c 'for i in $(seq 1 30); do curl -fs http://127.0.0.1:${MERCURE_GATEWAY_PORT}/ >/dev/null 2>&1 && exit 0; sleep 1; done; exit 1'

and `systemctl --user daemon-reload && systemctl --user restart mercure-gateway`.

## 5. Hardening — what is on, what is off, and why

| Directive | Setting | Why |
|-----------|---------|-----|
| `NoNewPrivileges`, `RestrictRealtime`, `LockPersonality`, `ProtectKernel*`, `ProtectControlGroups`, `PrivateTmp` | on | Standard sandboxing; no gateway feature depends on them. |
| `RestrictAddressFamilies` | `AF_INET AF_INET6 AF_UNIX` | DICOM/hub traffic + session D-Bus for the keyring. |
| `Restart`, `RestartSec`, `TimeoutStopSec` | on | Supervision and clean shutdown of the store-and-forward pipeline. |
| `ProtectHome` / `ProtectSystem=strict` | **off** | The spool, hub outbox, audit-head anchor and keyring fallback cache live under `$HOME` (default data dir `~/.local/share/mercure-gateway`) and must stay writable. |
| `PrivateDevices` | **off** | USB-dongle mode must reach the real `/dev` block devices (S10). |

## 6. Running headless without a desktop session

User units stop when the user session ends unless lingering is enabled:

```bash
loginctl enable-linger <user>     # services keep running after logout
```

After a log out/log in cycle the unit restarts with the booted config, the
durable hub outbox (`hub_outbox`, TD-06) resumes undelivered audit events, and
the spool resumes pending routing.

## 7. Troubleshooting

-   **Unit won't start, `ExecStartPost` failed** → gateway is up but the probe
    can't reach it. Check `journalctl --user -u mercure-gateway` for bind errors
    and confirm `MERCURE_GATEWAY_PORT` matches the port actually listening.
-   **`uv run: error: Failed to find project`** → `WorkingDirectory` points at a
    directory without the project. Fix `WorkingDirectory=%h/...` in the unit or
    install via `uv tool install .` and switch `ExecStart` to the `mercure-gateway`
    binary path.
-   **Keyring errors on first boot** → the session D-Bus may not be available;
    the gateway falls back to its file-based credential store automatically
    (see `docs/guides/secrets-and-env-overrides.md`).
-   **`curl: (7) Failed to connect` for 30s** → web admin failed to bind
    (port in use, `--port` mismatch, config invalid). Consult the journal first.