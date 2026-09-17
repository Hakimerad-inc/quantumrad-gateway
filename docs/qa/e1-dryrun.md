# E1 Site-Deployment Dry Run — v1.1.0-rc1

**Date:** 2026-09-16 · **Box:** `dev@linux workstation` (the "non-clinical VM" stand-in)
**Artifact:** the *published* `v1.1.0-rc1` GitHub release deb
(`QuantumRAD-Gateway_1.1.0-rc1_amd64.deb`, 70,061,720 B), downloaded fresh
from the release page and signature-verified before use — not a local build.
**Procedure:** `docs/guides/site-deployment.md` §§2–7, golden-config path.

The point of a dry run is to find what the test suite cannot. It found a
**release-blocking product defect in the shipped rc1 binary**.

## 0. Artifact provenance (site-deployment §2)

- Downloaded from `https://github.com/Hakimerad-inc/quantumrad-gateway`
  release `v1.1.0-rc1` via authenticated `gh release download`.
- `.sig` sidecar verified: minisign **PreHash** (magic `ED`) Ed25519 over
  `blake2b-512` of the artifact, keyid `af58fc682cd6b035` — matches the
  release-runbook §0.1 custody record. **Artifact signature: VALID.**
- Note: the trusted-comment *global* signature in the tauri-cli `.sig` does
  not verify under the minisign spec's NUL-terminated convention (tried
  bare, `\n`-terminated, and `\0`-terminated). The **artifact** signature —
  the one the Tauri updater actually checks — is valid; the global-sig
  mismatch is a tauri-cli comment-signing quirk worth reporting upstream, not
  a trust failure. `latest.json` embeds the same sidecars verbatim.

## 1. Deploy (§2–§3)

Extracted the deb to `/home/dev/e1-dryrun/root/` and launched the frozen
PyInstaller sidecar directly (`.../binaries/mercure-gateway/mercure-gateway`)
with a golden config — the fleet path, no wizard:

- receiver `:11113` (AET GATEWAY), web panel `127.0.0.1:8082` auth-off
  (loopback, per §5), destination `orthanc-testrig` → `127.0.0.1:4242` AET
  ORTHANC (the Sprint 01 test PACS), `auto_enqueue_delay_sec 1.0`
- Config gotcha hit and fixed: `config_version` must be a **string**
  (`"1"`, not `1`). My hand-written golden config used an unquoted `1` and
  the frozen binary exited with a pydantic `string_type` ValidationError.
  This is correct strict typing, not a product bug — the schema rejects the
  bad value loudly. The fix is documentation (site-deployment §3 now quotes
  it explicitly), since a fleet template authoring the JSON by hand is the
  realistic failure mode.
- Boot: health `{"status":"ok","version":"1.1.0-rc1"}`; startup C-ECHO to
  the destination succeeded (latency 28 ms, `health.status ok`).

## 2. The defect: a burst loses every study but the last

Sent three distinct single-instance studies back-to-back (one association
each, as a modality sends) inside one debounce window, using
`demo/fake_modality.py`:

```
study 1.2.840.10008.999.1.21: {'success': 1, 'failure': 0}
study 1.2.840.10008.999.1.22: {'success': 1, 'failure': 0}
study 1.2.840.10008.999.1.23: {'success': 1, 'failure': 0}
```

All three C-STOREs were **accepted** (store-before-ack, K1 held — no data was
lost). But the gateway only ever routed the last one:

| Study | Gateway state | Routes | In Orthanc? |
|---|---|---|---|
| `.21` | RECEIVED | **0** | no |
| `.22` | RECEIVED | **0** | no |
| `.23` | SENT | 1 | yes |

Confirmed independently on both sides: `/api/studies` shows the pair stranded
in RECEIVED with zero routes, and the Orthanc REST expansion contains only
`.23`.

**Root cause** (fixed in `553b718`): the auto-enqueue debounce held a single
Spool-wide timer slot. Each `store_instance` *replaced* the pending timer, so
study N's arrival cancelled study N-1's countdown. Any burst of small studies
— exactly what a modality emits as a stat batch, and exactly what a site
sends during a go-live flood — stranded all but the final study in RECEIVED
**indefinitely**, with no route ever created.

This is why the dry run mattered: the suite passed 712 tests green with this
bug aboard, because every existing test sent isolated studies. The per-study
regression tests added in `tests/test_auto_enqueue.py` (burst, `stop()`
cancellation) now pin it.

### Second finding: no operator recovery path

`POST /api/studies/{id}/retry` on the stranded pair returns
`400 "Study has no incomplete routes to retry"` — `reforward_study` iterates
*existing* routes, and these studies have none. There is **no web-panel way
to rescue a route-less RECEIVED study**: the spool holds the DICOM files
safely (nothing is lost), but delivery requires direct DB intervention.

`553b718` prevents the condition from arising, so this is defense-in-depth,
not a blocker. **Fixed anyway** in this dry run's follow-up (same commit
batch): `Spool.enqueue_study()` + `POST /api/studies/{id}/enqueue`. The
endpoint routes a RECEIVED study to the enabled destinations, is idempotent
(`already-queued`), refuses terminal (SENT) studies with 409, and surfaces
"no enabled destination" as 409 rather than a silent 200.

Verified live: `.61` stranded RECEIVED with 0 routes → `/retry` returns 400
(the documented dead end) → `/enqueue` returns `{"status":"queued"}` → study
reaches SENT and **lands in Orthanc**. TDD'd in `tests/test_manual_enqueue.py`
(6 tests) and `tests/test_web_api.py` (5 tests).

## 3. Fix verification (identical burst, fixed main)

Same golden config, same destination, same burst pattern, main @ `553b718`:

```
study 1.2.840.10008.999.1.31/32/33 → all {'success': 1, 'failure': 0}
queue stats: {"total":3,"sent":3,"error":0}
/api/audit/verify → {"valid":true,"errors":[]}
```

All three in Orthanc (`.31`, `.32`, `.33` all LANDED), `queue_depth`
`RECEIVED 0 / SENT 3 / ERROR 0`. The debounce now re-arms *per study*.

**Consequence for the release:** the published `v1.1.0-rc1` artifacts carry
the defect. rc1 must not go to a clinical site as-is; the fix rides the next
tag (`-rc2`), per plan risk R2.

## 4. Ops surfaces exercised (§6–§7)

- **Monitoring (D1):** `/api/system/metrics` exposed the defect precisely —
  `mercure_gateway_queue_depth{state="RECEIVED"} 2` against `SENT 1` is the
  signature an unmanned box would alert on. The backlog-growth alert rule in
  the admin guide would have fired. This is the concrete payoff of D1: a
  defect invisible to the suite was visible to a scrape.
- **Audit (K5):** `/api/audit/verify` `{"valid":true}` throughout, both
  before and after the fix — the chain stayed consistent while studies were
  stranded and when they flowed.
- **Security boundary (§5, D3b):** the on-site refusal check passed —
  `web_ui.host=0.0.0.0` + `auth_enabled=false` **refuses to boot** with an
  actionable message, and `MERCURE_GATEWAY_ALLOW_INSECURE_BIND=1` overrides
  it with a loud warning (the TLS-proxy / deliberate-deployment path).
  The deployed code is confirmed post-D3b.
- **Not exercised:** Prometheus scrape from an external job, the restore
  drill (D2), and the K4 wizard timing — the golden-config path skips the
  wizard by design; K4 needs the human UAT legs (§3 of rc1-checklist).

## 5. Verdict

| Check | Result |
|---|---|
| Release artifact provenance (signed, custody key) | ✅ |
| Golden-config deploy → health ok | ✅ (after `config_version` string fix) |
| Destination C-ECHO before forwarding | ✅ (28 ms) |
| C-STORE accepted / persisted-before-ack (K1) | ✅ |
| Burst → all studies forwarded | ❌ **rc1 ships the auto-enqueue defect** |
| Operator recovery of stranded study | ❌ no endpoint exists |
| Fix on main closes it | ✅ 3/3, audit valid |
| Audit chain valid (K5) | ✅ |
| Metrics expose the failure (D1) | ✅ |
| D3b bind-refusal + escape hatch | ✅ |

**Outcome: dry run FAILED rc1 as-shipped — correctly, and for the right
reason.** rc2 must carry `553b718`; re-run this dry run's §2 burst against
the rc2 artifact as the rc2 go/no-go gate (it is a 3-minute test once the
sidecar is up).

## 6. rc2 go/no-go gate — PASSED (2026-09-17)

The gate re-run, against the **published rc2 artifact** (not a source
checkout): the PyInstaller sidecar extracted from
`QuantumRAD-Gateway_1.1.0-rc2_amd64.deb` — the same bundle a site would
install — reporting `version 1.1.0-rc2`.

- **Provenance first.** `scripts/verify_release_sig.py` over the downloaded
  `.deb` against the runbook §0.1 custody key: **verifies** (the deb was
  not eyeballed, it was checked). Installer 70,063,092 B; bundle artifact
  201,034,261 B — inside the 250 MB K6 ceiling.
- **Identical conditions to the failing run:** golden config shape, same
  `orthanc-testrig` destination, `auto_enqueue_delay_sec: 30`, three
  single-instance synthetic studies sent back-to-back, one association
  each, inside one debounce window (`demo/fake_modality.py`, UIDs
  `.31/.32/.33`).

```
Sent synthetic study '1.2.840.10008.999.1.31': {'success': 1, 'failure': 0}
Sent synthetic study '1.2.840.10008.999.1.32': {'success': 1, 'failure': 0}
Sent synthetic study '1.2.840.10008.999.1.33': {'success': 1, 'failure': 0}
queue stats: {"total":3,"queued":0,"sending":0,"sent":3,"error":0,"failed":0}
/api/audit/verify → {"valid":true,"errors":[]}
/api/studies → .31 SENT (1 dest) · .32 SENT (1 dest) · .33 SENT (1 dest)
```

**3/3 routed and delivered** — against rc1's 1/3. `SENT` is set only by
`Spool.complete()`, which the forwarder reaches after the destination
accepts the C-STORE and which requires *all* routes complete, so the state
is destination-confirmed, not a local optimism. The per-study debounce
re-arm holds across the burst.

**Verdict: rc2 PASSES the go/no-go gate.** The artifact carries the fix and
behaves; the release-blocking defect that grounded rc1 is closed on the
thing that ships. Remaining rc2→GA gates are the human legs (§3 UAT, D5
booking) and the D2 finding triage — not this defect.
