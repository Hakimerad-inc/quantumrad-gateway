#!/usr/bin/env bash
# Release-day rehearsal: build + sign a Linux deb with the real updater key
# (docs/dev/release-runbook.md §2–§3). Prompts for the signer password once,
# keeps it in this process's environment only (never argv, never a file).
#
# Proves the full chain offline before the GitHub tag push:
#   PyInstaller sidecar (scripts/package_backend.py, run first) →
#   cargo tauri build --bundles deb with tauri.release.conf.json overlay →
#   deb + .sig sidecar emitted → assemble latest.json (printed, not written).
#
# Requires: ~/.tauri/mercure-gateway.key (runbook §0.1), tauri-cli v2,
# the SPA built into src/mercure_gateway/web/static/.
set -euo pipefail
root="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"

[ -f "$root/src-tauri/binaries/mercure-gateway/mercure-gateway" ] \
  || { echo "error: frozen sidecar missing — run: uv run python scripts/package_backend.py" >&2; exit 1; }
[ -f "$root/src/mercure_gateway/web/static/index.html" ] \
  || { echo "error: SPA not built — run: (cd web && npm run build)" >&2; exit 1; }

overlay="$root/src-tauri/tauri.release.conf.json"
pubkey_file="$HOME/.tauri/mercure-gateway.key.pub"
[ -f "$pubkey_file" ] || { echo "error: $pubkey_file missing (runbook §0.1)" >&2; exit 1; }

# Build the overlay exactly as release.yml does from the secret, then restore.
backup="$(mktemp)"
cp "$overlay" "$backup"
trap 'cp "$backup" "$overlay"; rm -f "$backup"' EXIT
jq --arg pk "$(cat "$pubkey_file")" '.plugins.updater.pubkey = $pk' "$backup" > "$overlay"

export TAURI_SIGNING_PRIVATE_KEY="$(cat "$HOME/.tauri/mercure-gateway.key")"
read -rs -p "Tauri signer password: " TAURI_SIGNING_PRIVATE_KEY_PASSWORD
echo
export TAURI_SIGNING_PRIVATE_KEY_PASSWORD

( cd "$root/src-tauri" && "$HOME/.cargo/bin/cargo-tauri" tauri build \
    --bundles deb --config tauri.release.conf.json )

echo "── artifacts + updater manifest rehearsal ─────────────────────────"
find "$root/src-tauri/target/release/bundle" -name '*.deb' -o -name '*.sig' | sort
python3 - "$root" <<'PY'
import glob, json, os, sys
from datetime import datetime, timezone
root = sys.argv[1]
base = "https://github.com/mercure-imaging/mercure-gateway/releases/latest/download"
platforms = {}
for sig in glob.glob(os.path.join(root, "src-tauri/target/release/bundle/**/*.sig"), recursive=True):
    name = os.path.basename(sig[:-4])
    key = "windows-x86_64" if name.endswith((".exe", ".msi")) else "linux-x86_64"
    platforms[key] = {"signature": open(sig).read(), "url": f"{base}/{name}"}
manifest = {
    "version": "1.1.0-rc1",
    "notes": "QuantumRAD Gateway 1.1.0-rc1 (local rehearsal)",
    "pub_date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "platforms": platforms,
}
print(json.dumps(manifest, indent=2)[:800])
assert platforms, "no .sig sidecars produced — createUpdaterArtifacts not active?"
print("\nlatest.json assembly: OK (linux-x86_64 present; windows leg from CI build job)")
PY
echo "next: uv run python scripts/check_installer_size.py <deb> --limit-mb 500"
