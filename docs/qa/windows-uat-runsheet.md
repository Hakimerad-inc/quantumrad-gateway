# Windows 10/11 clean-VM UAT — one-page run sheet

Executes `docs/qa/uat-06.md` (K4 timing) plus the §3 rows the checklist still has
open on Windows: service mode (S07-T9), tray/spawn, K6 RAM, and the Tauri half of
the auto-update tamper check. Evidence row at the end pastes straight back into
`docs/qa/rc1-checklist.md` §3/§4.

**Fill before you start:** tester, date, gateway version (the rc you downloaded).

## 0. Pre-flight (do this before opening the VM)

- [ ] Clean Windows 10/11 x64 VM, **no prior gateway install**, admin account available.
- [ ] **VM allows outbound HTTPS to `github.com`.** The in-app updater leg (§6) pulls
      a manifest from the release; clinical/host VLANs commonly block it. If the VM
      can't reach GitHub, do the rest and skip §6 — but say so in the evidence table,
      because §6 is the one row no CI job can substitute for.
- [ ] Download from the GitHub release page: `QuantumRAD-Gateway_<ver>_x64-setup.exe`
      and its `.sig` sidecar (optional here; §6 verifies signatures for real).
- [ ] A destination to send to: the `test-rig/` Orthanc (`docker compose up` on a
      Linux host reachable from the VM) or any PACS that will answer C-ECHO.
- [ ] Timer ready — K4 is a timed gate.

## 1. Install + wizard — K4 ≤ 10 min (uat-06.md §1–3)

Start the timer when the installer finishes.

| Step | Expected | Result |
|---|---|---|
| Run `..._x64-setup.exe` | Dialog appears, no UAC error | ☐ |
| Accept default path, finish | Desktop icon; receiver not yet started | ☐ |
| Installer size | ≤ 250 MB (K6 size half) | ☐ |
| Launch app | Window opens; tray icon appears (see §2) | ☐ |
| Browser → `http://localhost:8080` | Dashboard loads, receiver + forwarder "running" | ☐ |
| **Setup** → defaults (`GATEWAY` @ 11112) → **Next** | Wizard advances | ☐ |
| Destinations empty → **Next** | Validation error: "at least one destination required" | ☐ |
| Add destination (host/port/AET) → **Echo** | Badge "ok", or "refused" if none reachable | ☐ |
| Reports disabled → **Next** → **Save Configuration** | "Setup Complete"; config persisted | ☐ |
| Send a study to `GATEWAY@<vm-ip>:11112` | Receiver logs the C-STORE | ☐ |
| Queue tab | Study appears, advances to `complete` at the destination | ☐ |

**Stop the timer.** Record the elapsed time — K4 budget is ≤ 10 min. Then the
admin-panel sweep (uat-06.md §4): Config tab shows JSON; Logs tail live; Audit tab
shows the receive + setup events; **Verify Chain Integrity** → "✓ Chain intact".

## 2. Tray + sidecar spawn (§3 row, packaged-sidecar path)

The desktop shell spawns the bundled Python backend as a sidecar; this path only
ever runs in a real install.

| Step | Expected | Result |
|---|---|---|
| Tray icon present in the notification area | Icon renders | ☐ |
| Icon state while idle | idle glyph | ☐ |
| Send a study | Glyph transitions to sending, back to idle on completion | ☐ |
| Kill the destination / use a bad AET | Glyph transitions to **error**, not stuck sending | ☐ |
| Restore destination, retry | Returns to idle after success | ☐ |

The equivalent Linux leg is green (`rc1-checklist.md:41`); the Windows sidecar
path has **never executed anywhere**, so treat a failure here as expected, not
exceptional — see the note at the bottom.

## 3. Service mode — S07-T9

From the admin panel (or the service commands the panel exposes):

| Action | Expected after-state | Result |
|---|---|---|
| Install service | Service registered, not running | ☐ |
| Start | Backend up, health endpoint responds | ☐ |
| Stop | Backend exits cleanly, tray shows down | ☐ |
| Uninstall | Service removed, no orphan files in the data dir | ☐ |

## 4. K6 RAM ≤ 150 MB

Measure both processes — the Tauri shell and the bundled sidecar backend — and
record each separately; the ≤ 150 MB gate applies to the backend (the shell is a
browser-context desktop app and is budgeted by the K6 installer-size row instead):

```powershell
Get-Process | Where-Object { $_.ProcessName -like "*gateway*" -or $_.ProcessName -like "*mercure*" } |
  Select-Object ProcessName, Id, @{N="MB";E={[math]::Round($_.WorkingSet64/1MB)}}
```

Record the steady-state number with the receiver idle, and again mid-transfer.
Note: K6 RAM was previously only measurable on Windows; the CI size gate
(K6-installer) is already green.

## 5. Auto-update offer + tamper check — Tauri desktop half

The headless/Python half is proven (`scripts/rehearse_updater_tamper.py`: genuine
sig staged, bit-flipped sig refused, nothing staged). This is the in-app half:

1. With the VM's outbound HTTPS working, check for updates in-app → it either
   offers the current/newer signed build or reports "up to date" — not an error.
2. Serve a mutated `latest.json` / bit-flipped `.sig` from a staging URL (the
   runbook §3 recipe), point `config.update.update_url` at it, trigger the check.
3. **Expected: update refused, nothing staged, no crash.** Record the refusal.

## Evidence record (paste back into rc1-checklist.md)

| Gate | Criterion | Observed | Pass |
|---|---|---|---|
| K4 | install → first study ≤ 10 min | _mm ss_ | ☐ |
| K6 size | installer ≤ 250 MB | _MB_ | ☐ |
| K6 RAM | backend ≤ 150 MB steady / mid-transfer | _MB / _MB | ☐ |
| §3 tray | icon renders + 3 states distinct | | ☐ |
| §3 service | install/start/stop/uninstall clean | | ☐ |
| §3 updater | tamper refused, nothing staged | | ☐ |
| §3 sidecar | spawned from packaged path on Windows | | ☐ |

## Note on the expected fix → rcN loop

The packaged-Windows sidecar path has never run anywhere, and the same class of
bug (a packaged-path assumption that tests don't reach) already bit once on Linux
— `553b718`, the Spool-wide auto-enqueue debounce that dropped bursts in the E1
dry-run. **Budget for one or two rebuild cycles rather than treating red as a
surprise.** When something fails: file the fix on `main`, re-verify gates locally,
and cut the next `-rcN`. Tags are immutable — never move a tag; cut a new one.
