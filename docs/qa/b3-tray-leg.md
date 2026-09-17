# B3 — Ubuntu Desktop GUI / Tray Leg — v1.1.0-rc2

**Date:** 2026-09-17 · **Box:** `dev@linux workstation` (GNOME 47, Wayland)
**Artifact:** the published `v1.1.0-rc2` deb — extracted, not installed — with the
sidecar and Tauri shell run from the extracted tree as a normal user.
**Runnable form:** the shell scripts under `/tmp/b3-tray/` (see §5).

Closes the `rc1-checklist.md` §3 row "Ubuntu … GUI boot + tray forward leg".
The substantive half ran 2026-09-14 (rc1); this leg is the visual-glyph
confirmation the checklist left open, run against rc2.

## Isolation

The B3 instance never touches the live dev gateway on `:8081`:

| | B3 (throwaway) | Live node (untouched) |
|---|---|---|
| HOME | `/tmp/b3-tray/home` | `/home/dev` |
| Web | `127.0.0.1:18080` | `127.0.0.1:8081` |
| Receiver | `11143` | `11112` |
| Config | `/tmp/b3-tray/gateway.json` | repo `mercure-gateway.json` |

Verified after every restart: `:8081` still reported `1.1.0-rc1` throughout;
`:18080` reported `1.1.0-rc2`. Anchor paths are `$HOME`-keyed, so the drill's
`audit-heads.txt` never touched the live node's.

## Results

| Check | Result |
|---|---|
| Deb extracts; sidecar spawns from the packaged path | ✅ |
| Health + correct version | ✅ `{"status":"ok","version":"1.1.0-rc2"}` |
| Tray icon registers with the session | ✅ StatusNotifierItem `tray_app_<pid>_1` |
| **Idle glyph renders** | ✅ green ring `(16,185,129)` |
| **Sending glyph renders, distinct** | ✅ amber ring + dot `(245,158,11)` |
| **Error glyph renders, distinct** | ✅ red ring + exclamation `(220,38,38)` |
| Study forwarded to a real PACS | ✅ 120-instance study → SENT, Orthanc accepted |
| Glyph returns to idle after completion | ✅ |
| Audit chain valid throughout | ✅ `{"valid":true,"errors":[]}` |

`docs/qa/evidence/b3-tray-states.png` is the three-glyph composite (8× upscale).

### The transition trace

Glyphs were read live from the ayatana StatusNotifierItem over D-Bus
(`IconName` resolves to a temp PNG the shell rewrites on each state change, so
each sample is a fresh copy, not a cached path). Sending 120 instances gave a
long enough transfer window to catch the transient state:

| Sample | sha-16 | queue | state |
|---|---|---|---|
| live-1 … live-10 | `50f5439423fe8c26` | empty → `queued 1` | idle |
| live-11 … live-21 | `c755e8ce851abb7f` | `sending 1` | **sending** |
| live-22 … live-25 | `50f5439423fe8c26` | `sent 1` | idle (returned) |

Pairwise pixel diffs confirm the glyphs are not near-duplicates:
idle↔sending 312/1024 px, idle↔error 322, sending↔error 334.

## Finding: a dead backend rendered a login screen (FIXED)

**Severity: operator-confusing, not data-facing. FIXED 2026-09-17 (`b0e74d4`).**

The SPA's session probe treated a transport failure and a 401 identically —
both set `isAuthenticated = false`, so `App` rendered `LoginView`. When nothing
was listening on the backend port, an operator stared at a password prompt,
would type a valid password, and get "Network error" — which reads like a wrong
password. No credential can fix a refused connection.

This leg hit it directly: the shell injects `__MERCURE_PORT__` from
`MERCURE_BACKEND_PORT`, the sidecar had died on a config-resolution error, and
the window opened on a login screen for a backend that was never there.

The fix splits the two cases — `fetch` rejects only on transport failure, never
on an HTTP status, so the distinction is exact:

- `AuthContext` tracks `backendUnreachable` separately; `login()`'s catch sets
  it too, so a submit against a dead backend leaves the login screen instead of
  looping on it.
- New `BackendDownView` names the probed host:port and offers a Retry — **no
  password field**. `App` checks it before the not-authenticated branch.
- 6 new frontend tests (20/20, was 14): the three-way split 200 / 401 /
  connection-refused, plus that the connection view renders no password form.

Verified live in a real browser both ways: against a dead port it renders
"Gateway unreachable" naming `:59999` with no credential prompt; against the
live B3 backend it renders the dashboard directly, no login step.

**Post-rc2, like the `9c10423` audit fix.** The rc2 deb's bundled
`_internal/.../web/static` is frozen at tag time and still carries the login
screen for this case; the fix rides the next tag. This was confirmed by
overlaying the rebuilt bundle into the extracted deb and reloading — the
behavior difference is entirely the bundle, not the backend.

## Caveats

- The glyphs were verified programmatically (D-Bus icon bytes + pixel diff),
  not by a human eye on the panel. The evidence PNG is in the repo for the
  visual sign-off; appindicator was live in the session and the icon registered
  with the GNOME shell's StatusNotifierWatcher.
- The destination was the test-rig Orthanc (`127.0.0.1:4242`, AET ORTHANC).
  Its REST on `:8042` is auth-gated, so "landed" was confirmed gateway-side
  (`SENT` is set only by `Spool.complete()` after the destination accepts the
  C-STORE) rather than by an Orthanc REST expansion.
- Config gotcha hit again, same as E1: `config_version` must be a **string**.
- The sidecar ignores `MERCURE_GATEWAY_CONFIG` as a *path* override; it must be
  launched with `--config <path>`. Without it the binary falls back to
  `./mercure-gateway.json` relative to its CWD and silently binds defaults —
  which is how this leg's first attempts collided with the live node's `11112`.
  Worth a sentence in the admin guide's troubleshooting section.

## 5. Runnable form

The drill is a set of scripts under `/tmp/b3-tray/` (not committed — they are
this box's throwaway paths):

- `reset.sh` — stop any prior instance, wipe spool + anchors, start the sidecar
  and the shell, print both pids.
- `icon.sh <shell-pid> <name>` — read the current tray glyph over D-Bus, diff
  it against the named snapshot, and report the sha + pixel delta.
- `start_backend.sh` — sidecar only, for the window/Web legs.

The D-Bus detail that made icon sampling reliable: the ayatana temp path
carries an incrementing serial *and* the bus owner name changes per shell
instance, so both the owner and the path must be resolved live.

## Related

- `docs/qa/rc1-checklist.md` §3 — the row this closes
- `docs/qa/e1-dryrun.md` §6 — the rc2 go/no-go gate this artifact passed
- `docs/qa/human-gates-plan.md` — where #6 sits in the release board
