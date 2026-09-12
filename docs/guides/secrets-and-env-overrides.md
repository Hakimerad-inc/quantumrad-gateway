# Secret Inventory & Environment Overrides (TD-08)

This guide lists every credential the gateway holds, where it can live, and how
operators inject production secrets without ever writing them to a config file.

## Secret inventory

| Secret | Config path | Storage today | Redacted by API |
|--------|-------------|---------------|-----------------|
| Hub/band audit API key | `audit.hub_reporting.api_key` | `mercure-gateway.json` (plain or AES-256-GCM vault in `credentials.entries`) | Yes |
| Web admin login password | `web_ui.auth_password_hash` | `mercure-gateway.json` (plain or vault) | Yes |
| Hub anchor signing key | `audit.hub_reporting.anchor_public_key` | `mercure-gateway.json` | Yes (sentinel round-trip) |
| Destination passwords / keys | `destinations[N]` (`password`, `private_key`, `passphrase`, `secret_access_key`, `access_key_id`, `auth_token`) | Keyring (with filesystem fallback cache) or `credentials.entries` vault | Yes |
| XNAT password | `destinations[N].password` | Keyring or vault | Yes |
| Encrypted credential blocks | `credentials.entries.*` (`password_encrypted`, `private_key_encrypted`, `passphrase_encrypted`, `api_key_encrypted`) | `mercure-gateway.json` | Yes |
| DB at-rest encryption key | n/a (derived from `MERCURE_MASTER_PASSWORD`) | Environment only | n/a |
| Self-update public key | `update.public_key` | `mercure-gateway.json` | Yes |
| DICOM AE-titles | `receiver.ae_title`, destinations `aet_*` | Config | No (not secret-class) |

Credential storage locations, in decreasing precedence:

1. **Environment** (`MERCURE_MASTER_PASSWORD*`, and the `MERCURE_GATEWAY_*`
   overrides below) — never touches disk.
2. **Keyring** — the OS credential store (gnome-keyring / KWallet / macOS
   Keychain / Windows Credential Manager). Used for destination credentials at
   runtime; backed by `~/.local/share/mercure-gateway` (`mercure_gateway.keyring_store`)
   when no keyring backend is available.
3. **Encrypted vault** — `credentials.entries` in the config file, AES-256-GCM
   sealed under `MERCURE_MASTER_PASSWORD` (PBKDF2 100k). Portable, air-gapped
   fallback (`mercure_gateway.config.encryption`). On disk, sealed secret fields
   are replaced with the non-secret `__ENCRYPTED_AT_REST__` placeholder.
4. **Plaintext** — accepted on first setup (wizard), but overwritten at rest
   encryption time. Never leave production secrets in plaintext.

## Environment variable overrides

The gateway follows 12-factor configuration: `MERCURE_GATEWAY_*` variables are
applied **after** the config file is loaded, so a deployment can inject secrets
(or non-secrets) without a deploy-time file edit
(`mercure_gateway.config.apply_env_overrides`).

Variable naming: `MERCURE_GATEWAY_` + dotted config path in `SCREAMING_SNAKE`.
Every scalar string/int/bool field is eligible; `destinations[]`/lists are
skipped (the config file owns them).

### Secret fields you should supply from the environment

```sh
# Hub/band audit reporting API key (TD-08)
MERCURE_GATEWAY_AUDIT_HUB_REPORTING_API_KEY=top-secret-key
MERCURE_GATEWAY_AUDIT_HUB_REPORTING_BOOKKEEPER_URL=https://hub.example.com
MERCURE_GATEWAY_AUDIT_HUB_REPORTING_ENABLED=true

# Web admin login password hash (bcrypt/sha256$salt$hash).
# Generate with the setup wizard, or: htpasswd -bnBC 10 "" 'password' | cut -d: -f2
MERCURE_GATEWAY_WEB_UI_AUTH_ENABLED=true
MERCURE_GATEWAY_WEB_UI_AUTH_PASSWORD_HASH=sha256$salt$hash

# Master password for the at-rest config vault.
# MERCURE_MASTER_PASSWORD_FILE=/run/secrets/gateway-master-pw   (preferred: file)
# MERCURE_MASTER_PASSWORD=...                                   (or inline)
```

### Precedence

```
config file  →  MERCURE_GATEWAY_* overrides  →  runtime keyring lookup
```

Env overrides only touch the fields they name — unrelated credentials in
`web_ui`/`audit`/`credentials` are left untouched. Overriding the hub API key
via env still requires the file to be readable: an encrypted config must first
unseal with `MERCURE_MASTER_PASSWORD`, otherwise `load_config` raises before
the override runs.

## Operational notes

- **Never commit secrets.** Committed fixtures (`e2e/seed.py`, `test-rig/`)
  are demo/test values only, never production credentials.
- **save_config caveat:** an env-injected secret is present on the in-memory
  model; if the web admin **Config → Save** is used, the current value is
  serialized back to disk. Keep `credentials.encrypted=true` (default) so a
  save re-seals it under the master password instead of writing plaintext.
- **Rotation:** set the new value in env, restart the service (systemd unit:
  `systemctl --user restart mercure-gateway`); old value never appears on disk.
- **Audit:** the web API redacts every secret in `GET /api/config` and audit
  exports (`redact_config`, `***` sentinel) — env-supplied secrets are
  included in that redaction automatically.