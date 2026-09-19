# Release Runbook — v1.1.0-rc1 (and later tags)

Release-day sequence for the tag→build→sign→publish pipeline
(`.github/workflows/release.yml`). Read `docs/dev/packaging.md` first for the
*why* (freeze/bundle/sign architecture); this file is the *do*.

## 0. Prerequisites (one-time)

### 0.1 Updater signing key — custody record

The keypair was generated on this dev box (`feat/rc-readiness` work,
2026-09-14):

```bash
cargo tauri signer generate -w ~/.tauri/mercure-gateway.key   # password-protected
```

| Field | Value |
|---|---|
| Key file | `~/.tauri/mercure-gateway.key` (private, **never committed, never in CI logs**) |
| Public key file | `~/.tauri/mercure-gateway.key.pub` |
| Public key (minisign) | `dW50cnVzdGVkIGNvbW1lbnQ6IG1pbmlzaWduIHB1YmxpYyBrZXk6IDM1QjBENjJDNjhGQzU4QUYKUldTdldQeG9MTmF3TlpDTUJlQkE4ZUJCeU9PWnNJbHpsYXpleEU4eEM4dnJadHJ5d0lKdW0xbjcK` |
| Key-file SHA-256 fingerprint | `e4aac23b3651017b7779b25579a3971605ae8ccc3d7a0094ece009fe4ec9e5ab` |
| Password custody | shown once at generation (2026-09-14) — **store in maintainer password manager now** (entry: "mercure-gateway tauri signer"); not in the repo, not in shell history files |
| Generated | 2026-09-14, machine `dev@linux workstation`, tauri-cli 2.11.4 |
| Backup | copy key + pub + password into the org password manager; the ONLY copies |

**Rotation:** if the box is lost, generate a new keypair, publish a full
installer out-of-band (updaters signed with the old key stop verifying), and
update the `MERCURE_TAURI_PUBLIC_KEY` secret. There is no in-band recovery:
the updater only trusts the compiled-in pubkey.

### 0.2 GitHub repository + secrets

The remote is live: `https://github.com/Hakimerad-inc/quantumrad-gateway.git` (pushed 2026-09-14). Remaining on release day:

```bash
git remote add origin https://github.com/Hakimerad-inc/quantumrad-gateway.git
gh repo create Hakimerad-inc/quantumrad-gateway --private --source=. --push  # or existing repo
gh secret set MERCURE_TAURI_PRIVATE_KEY          < ~/.tauri/mercure-gateway.key
gh secret set MERCURE_TAURI_PRIVATE_KEY_PASSWORD   # prompt, value = signer password
gh secret set MERCURE_TAURI_PUBLIC_KEY           < ~/.tauri/mercure-gateway.key.pub
```

Secret names must match `release.yml` exactly (`MERCURE_TAURI_PRIVATE_KEY`,
`MERCURE_TAURI_PRIVATE_KEY_PASSWORD`, `MERCURE_TAURI_PUBLIC_KEY`). The
`tauri.conf.json` updater endpoint already points at
`github.com/Hakimerad-inc/quantumrad-gateway` — if the org/repo name differs,
update `plugins.updater.endpoint` there first.

## 1. Pre-tag checks (local, every release)

```bash
uv run pytest -q                      # green (known-load flakes: see §4)
uv run python scripts/sync_version.py --check   # all six sources agree
uv run mypy src/mercure_gateway && uv run ruff check .
cd web && npm run build && cd ..      # SPA fresh into src/mercure_gateway/web/static
```

Tick everything tickable in `docs/qa/rc1-checklist.md` §1/§2/§4 and record
measured numbers. The tag should be the tip commit of a green main.

## 2. Tag and trigger

```bash
git tag -s v1.1.0-rc1 -m "v1.1.0-rc1"     # signed tag preferred
git push origin main --follow-tags         # push fires release.yml
gh run watch                               # watch build → release jobs
```

Local rehearsal (run before the tag): `scripts/rehearse_signed_build.sh`
prompts for the signer password, builds + signs the deb with the real overlay,
and rehearses `latest.json` assembly. Proven green 2026-09-14: deb 69.6 MB
(K6 gate 500 MB) with its `.sig` sidecar. Requires the Tauri Linux system
deps, notably `libayatana-appindicator3-dev` (the bundler panics without its
pkg-config file even when the runtime library is present) — same set CI's
`package-linux` installs.

`release.yml` (two stages): per-OS **build** jobs freeze the PyInstaller
sidecar, build + sign the Tauri bundles (real pubkey +
`createUpdaterArtifacts` injected via the `tauri.release.conf.json` overlay
from secrets), and upload artifacts. The **release** job creates the GitHub
release (`gh release create --generate-notes`), merges BOTH platforms into one
`latest.json` (with `pub_date` defaulted to UTC-now — pin
`RELEASE_PUB_DATE` via workflow_dispatch only if you need a fixed stamp),
generates the CycloneDX SBOM for the frozen sidecar, computes sha256 checksums
of every artifact, and uploads installers + `.sig` sidecars + `latest.json` +
`sbom.json` + `SHA256SUMS`, printing the checksums into the release body.

## 3. Post-release verification

```bash
gh release view v1.1.0-rc1 --json assets -q '.assets[].name'
# expect: installer(s) per OS, matching .sig files, latest.json, sbom.json, SHA256SUMS
curl -fsSL https://github.com/Hakimerad-inc/quantumrad-gateway/releases/download/v1.1.0-rc1/latest.json | jq .platforms
# expect: BOTH windows-x86_64 and linux-x86_64 keys, non-empty signatures, valid pub_date
```

- Install the artifact on a clean VM (rc-checklist §3 rows) and exercise
  check-for-updates: the installed version should see no newer release; then
  publish `v1.1.0` and confirm the updater offers it (signature enforced).
- **Integrity rehearsal:** the release body lists every artifact's sha256.
  Download one installer plus `SHA256SUMS` into the same directory and run
  `sha256sum -c SHA256SUMS` — this is the only channel that proves a download
  against a value readable *before* fetching anything (a signature proves
  "this key signed this artifact", not "you got what was published").
- **Dependency audit:** `sbom.json` is the CycloneDX 1.5 document for the
  frozen Python sidecar. Feed it to a scanner (`cyclonedx` CLI, Dependency
  Track, `syft`) to answer "which versions are in this appliance?" without
  diffing the repo at the tag. Its scope is the *resolved build input*
  (`uv export --frozen`), not a byte-exact bundle inventory — see the honest
  scope note in `scripts/export_sbom.py`. PyInstaller's build-time deps
  (`altgraph`, `macholib`, …) appear there and do not ship.
- **Tamper rehearsal (no Windows needed):** `update_url` is operator
  configurable (`config.update.update_url`) — point a test install at a local
  `python3 -m http.server` serving a hand-edited `latest.json` whose signature
  was mutated; the Python updater must refuse. The Tauri in-app updater path
  requires the desktop shell on a real install (§3).

## 4. Known CI caveats

- Full-suite wall-clock flake budget: a few timing-sensitive tests
  (`test_main.py` port-readiness, `test_receiver_wire` 25-association burst)
  can exceed per-test timeouts on a loaded free-tier Windows runner; they pass
  deterministically in isolation (verified 2026-09-14). If red in CI, re-run
  the job before treating it as a regression.
- AppImage bundling downloads linuxdeploy at build time — a flaky network can
  fail `package-linux`; the deb leg is authoritative for the size gate.

## 5. Re-dispatching a failed leg (non-destructive)

`workflow_dispatch` from main with the `tag` input re-runs the *current*
pipeline against an already-pushed (immutable) tag. This is how a failed
release leg is recovered.

The upload step is deliberately **not** `--clobber` (review P1-15): overwriting
a published artifact would also overwrite its `.sig`, so a fresh signature over
a *different* artifact would still verify against the same compiled-in pubkey —
trust would degrade to "the signing key was never misused". So:

- A re-dispatch that finds an asset it wants to upload **fails loudly** with an
  `::error` naming the release. It does not silently replace anything.
- To recover: delete the stale asset(s) on the release page by hand, then
  re-dispatch. Never re-publish over a signed artifact.
- Non-conflicting assets still upload, so a partial failure (e.g. only the
  Windows leg succeeded first time) does not need a full re-run.
- `latest.json` is regenerated each dispatch; re-dispatching both legs keeps it
  consistent. If only one leg re-ran, verify §3's `jq .platforms` shows both
  keys afterwards.

## 6. Rolling back a bad update

The gateway has **no in-process rollback** (review P1-14). The Tauri updater is
forward-only: a staged archive is swapped at the next restart and the running
binary cannot un-swap itself. `Updater.rollback()` existed until rc3 and had
zero production callers — it is deleted rather than left as reachable-looking
dead code an operator might plan around.

The real procedure, for a release that turned out to be broken after shipping:

1. **Unpublish the broken release first.** Delete the release (or the
   `latest.json` manifest pointing at it) so installed updaters stop
   offering it. This is the only step that protects boxes that have not
   updated yet — everything below is per-box, and has to be repeated for
   every appliance. On GitHub: `gh release delete <tag>` and, if the tag
   itself is what `update_url` resolves to, delete or re-point the tag.
2. **Stop the gateway** (`systemctl --user stop mercure-gateway`, or quit the
   desktop app).
3. **Reinstall the previous signed installer** for the platform — the one from
   the release you are rolling back to. The installer replaces the program
   tree only.
4. **Data and config survive**: the spool database, `operations.log`, the audit
   chain, `mercure-gateway.json`, and the credential store all live outside the
   program tree (see `docs/guides/backup-restore.md` for the paths). Nothing in
   step 3 touches them.
5. **Restart** and confirm the reported version in the web panel's footer or
   `GET /api/system/info`.
6. **Prevent re-application**: the desktop shell checks for updates on a
   schedule. If the broken release is still `latest`, point
   `config.update.update_url` at a manifest for the good version, or disable
   update checks (`config.update.enabled = false`) for a version-pinned
   deployment until the broken release is superseded.
7. **Tell the fleet**: if the broken release reached installed appliances, the
   fix is a *new* signed release (which the updater will accept as an upgrade),
   not a rollback instruction — installed updaters will keep offering whatever
   `latest.json` says.

`systemd Restart=always` does not help here: it restarts a *crashing* binary,
which is the wrong tool when the binary is new and wrong rather than dead.

## 7. After a successful RC

Update the sprint board (S09-T7 evidence line), `docs/qa/rc1-checklist.md`
header (date/captain), and note any §3 UAT results. The GA cut repeats §1–§3
at `v1.1.0` with the checklist fully ticked.
