# Phase 1.1A — Code Quality & Architecture Review

**Target:** dicom-gateway working tree at base commit `aab94b5` plus uncommitted
WIP (`web/` 11 modified files, untracked `axe.js` assets and
`web/src/test/a11y-scan.test.tsx`).
**Date:** 2026-09-18
**Scope:** complexity, maintainability, duplication, Clean Code/SOLID, technical
debt, error handling. Security implications are called out where they intersect
those dimensions (this is a PHI-handling system); a dedicated security pass
follows in a later phase.

## Gate health at review time

All existing quality gates pass, so the findings below are quality/correctness
issues, not broken builds:

| Gate | Result |
|---|---|
| `ruff check .` | clean |
| `mypy .` (strict) | clean (70 files) |
| `tsc -b` (web) | clean |
| `eslint src` (web) | clean |
| `vitest run` (web) | 11 files / 65 tests pass |
| built SPA in VCS | no (fixed since the 2026-09-08 review) |

---

# Critical

## C-1. Path traversal via server-controlled UIDs in DICOMweb report persistence

**Location:** `src/mercure_gateway/reports/dicomweb.py:177-184` (`_save`),
reached from `retrieve()` at `123-155`.

`_save` composes the output path straight from values parsed out of a remote
server's JSON response:

```python
@staticmethod
def _save(self, ds: Any, match: ReportMatch) -> Path:
    """Persist *ds* under ``reports/{study_uid}/{sr|pdf}/{sop_uid}.dcm``."""
    sub = "sr" if match.sop_class_uid == SR_SOP_CLASS else "pdf"
    out_dir = self._reports_dir / match.study_uid / sub
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{match.sop_instance_uid}.dcm"
    ds.save_as(str(path), enforce_file_format=True)
    return path
```

`match.study_uid` and `match.sop_instance_uid` come from `_json_ui()` on an
HTTP response body (`114-116`) — a remote DICOMweb server controls them
verbatim. `_UID_RE`-style validation is never applied. A response carrying
`"0020000D": {"Value": ["../../../../etc/cron.d"]}` or a `sop_instance_uid`
containing `../../` writes attacker-chosen DICOM bytes anywhere the gateway
process can write. The receive path solved exactly this threat
(`src/mercure_gateway/spool/__init__.py:62-70, 321-323`, documented in the
module docstring); the reports tree has the same exposure and no guard.

This is a maintainability finding as much as a security one: the invariant
"untrusted UIDs never reach the filesystem" is enforced in one subsystem and
silently absent from its sibling, which is the structural signature of a
missing shared boundary.

**Fix.** Reuse the existing validator and contain the resolution:

```python
from mercure_gateway.spool import validate_uid

def _save(self, ds: Any, match: ReportMatch) -> Path:
    study_uid = validate_uid(match.study_uid, what="StudyInstanceUID")
    sop_uid = validate_uid(match.sop_instance_uid, what="SOPInstanceUID")
    sub = "sr" if match.sop_class_uid == SR_SOP_CLASS else "pdf"
    out_dir = self._reports_dir / study_uid / sub
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{sop_uid}.dcm"
    if path.parent.resolve() != out_dir.resolve():
        raise DICOMwebError(f"refusing to write outside reports dir: {path}")
    ds.save_as(str(path), enforce_file_format=True)
    return path
```

`_save` should raise rather than silently misplace a file — `retrieve()`
already has a documented "no silent drop" contract (`124-127`) that this
strengthens. Consider moving `validate_uid` to a `mercure_gateway.uids` module
so both `spool` and `reports` import it as a peer rather than reports depending
downward into the spool package.

## C-2. Same traversal class over a DICOM association (C-FIND/C-MOVE)

**Location:** `src/mercure_gateway/reports/move.py:170-177` (`_save`), fed by
`src/mercure_gateway/reports/find.py:126-137`.

Identical defect, different transport: `ReportMatch.study_uid` /
`sop_instance_uid` are read off a PACS C-FIND response
(`str(getattr(dataset, "SOPInstanceUID", ""))`) and flow into
`self.reports_dir / study_uid / sub / f"{sop_uid}.dcm"` with no validation.

**Fix.** Same as C-1 — `validate_uid()` plus a resolved-parent containment
check, applied in `move.py:_save`. Fixing both call sites with the same helper
is what removes the duplication that let one of them stay vulnerable.

## C-3. Boot-time bind-security invariant can be downgraded at runtime, and auth cannot be enabled through any documented path

**Location:** `src/mercure_gateway/web/routes.py:769-801` (`update_config`) ×
`src/mercure_gateway/web/auth.py:88-105` (`require_auth`) ×
`src/mercure_gateway/main.py:99-130` (`_enforce_bind_security`).

The security architecture is internally inconsistent about *when* the
"auth-off implies loopback" invariant is evaluated:

- `main.py:99-130` refuses to boot when `auth_enabled` is false and the bind
  host is not loopback (escape hatch only via `MERCURE_GATEWAY_ALLOW_INSECURE_BIND=1`).
- `routes.py:790` then does `request.app.state.config = updated` on every
  `PUT /config`, and `auth.py:94-96` reads that same live object:

```python
config: GatewayConfig = request.app.state.config
if not config.web_ui.auth_enabled:
    return
```

So a single authenticated `PUT /config` with `web_ui.auth_enabled: false`
flips the running API to unauthenticated **without re-checking the bind host**.
The boot-time refusal the docstring in `auth.py:91-92` relies on
("loopback-only binding is enforced by the composition root instead") is
boot-time only. On an appliance bound to `0.0.0.0` this silently re-exposes
PHI, credentials, and component start/stop to the network for the rest of the
process lifetime — and it is saved to disk, so it survives the next boot only
if the bind happens to be loopback again.

Compounding this, there is **no operator-facing way to enable auth**:

- `main.py:127-129` tells a boot-failing operator to "Enable web_ui auth
  (wizard → Setup)".
- `docs/guides/admin-guide.md:58` says the same: *"Enable
  `web_ui.auth_enabled` and set a password hash via the **Setup**"*.
- But the wizard's steps are `["receiver", "destinations", "reports",
  "summary"]` (`src/mercure_gateway/web/wizard.py`,
  `web/src/pages/SetupWizard.tsx:5`). There is no auth step, and no CLI exists
  for it either. Only `docs/guides/secrets-and-env-overrides.md:61` documents
  a real workaround (`htpasswd -bnBC 10 "" 'password' | cut -d: -f2` plus
  hand-editing JSON).

The practical operator flow is therefore "turn auth off to get unblocked, and
never turn it back on," because turning it back on requires generating a hash
out-of-band. That is a design-level quality defect on a medical device, not a
config nit.

**Fix (both halves).**

1. Re-evaluate the invariant at write time, in `update_config`:

```python
# A runtime downgrade must clear the same bar boot-time does: auth-off is
# only safe on loopback (main._enforce_bind_security, see auth.py docstring).
if not updated.web_ui.auth_enabled and updated.web_ui.host not in _LOOPBACK_HOSTS:
    raise HTTPException(
        status_code=400,
        detail=(
            "Refusing to disable web_ui.auth_enabled while binding to "
            f"{updated.web_ui.host!r}: the admin API would be unauthenticated "
            "on the network. Bind to 127.0.0.1 first, or set "
            "MERCURE_GATEWAY_ALLOW_INSECURE_BIND=1 at boot."
        ),
    )
```

   (`_LOOPBACK_HOSTS` is already a module constant in `main.py` — move it to a
   shared location and import it from both, rather than re-declaring it.)

2. Add the missing capability: either a `security` step in the wizard
   (`wizard.py` step list + `SetupWizard.tsx`) that takes a plaintext password,
   hashes it server-side, and writes `auth_password_hash`, or a
   `mercure-gateway set-password` CLI subcommand. Until one of those exists,
   `main.py:127-129` and `admin-guide.md:58` are instructions to a feature
   that does not exist and should be corrected to point at the real procedure.

---

# High

## H-1. Forwarding requests >128 presentation contexts, making whole studies permanently undeliverable

**Location:** `src/mercure_gateway/forwarder/handlers/dicom.py:105-112`
(`_send_files`).

```python
contexts = list(sop_classes_for_files(files)) or list(_STORAGE_CONTEXTS)
for ctx in contexts:
    ae.add_requested_context(ctx, AllTransferSyntaxes)
for syntax in _COMPRESSED_SYNTAXES:
    for ctx in contexts:
        ae.add_requested_context(ctx, syntax)
```

This requests **N × 4** contexts (1 `AllTransferSyntaxes` + 3 dedicated
compressed syntaxes). Verified constraints:

- pynetdicom `AE.add_requested_context` raises `ValueError` once
  `len(self.requested_contexts) >= 128`
  (`pynetdicom/ae.py:256`, also `:181`, `:1588`).
- `STORAGE_SOP_CLASSES` has **111** entries (`sop_classes.py:146`), so the
  fallback branch requests **444** contexts.
- `sop_classes_for_files` (`sop_classes.py:284-300`) silently skips files it
  cannot read or whose SOP class is not in `_KNOWN`, so the fallback fires
  whenever a study's headers are unreadable or its classes are unusual.

Consequences, all bad for a clinical gateway:

- a study with unreadable/unusual headers is **never deliverable** — the
  negotiation itself throws;
- any study containing ≥33 distinct storage SOP classes (132 contexts) is
  never deliverable;
- `deliver()` only catches `ConnectionError` (`dicom.py:92`), so the
  `ValueError` escapes to `Forwarder._dispatch`'s broad handler boundary
  (`forwarder/__init__.py:222-227`) → retried `max_attempts` times → study
  ends `FAILED` with a message that says `ValueError: ...` and gives the
  operator no clue it is a negotiation-budget problem.

The module comment at `101-104` even acknowledges the 128-context budget while
implementing a multiplication that breaches it for the fallback case — a
comment/code drift that is itself a maintainability red flag.

**Fix.** Budget the contexts and shed dedicated syntaxes first:

```python
_BUDGET = 126  # pynetdicom rejects >= 128; leave headroom for the move ctx

def _send_files(self, files: list[Path]) -> None:
    ae = AE(ae_title=self.destination.aet_source)
    ae.maximum_pdu_size = _MAX_PDU_SIZE
    contexts = list(sop_classes_for_files(files)) or list(_STORAGE_CONTEXTS)
    syntaxes = [AllTransferSyntaxes, *_COMPRESSED_SYNTAXES]
    # Prefer dedicated per-syntax contexts, but shed them (cheapest first)
    # rather than blowing the protocol budget: a study with many SOP classes
    # must still be deliverable.
    while contexts and len(contexts) * len(syntaxes) > _BUDGET:
        if len(syntaxes) > 1:
            syntaxes.pop()
        else:
            contexts = contexts[: _BUDGET // len(syntaxes)]
    for ctx in contexts:
        for syntax in syntaxes:
            ae.add_requested_context(ctx, syntax)
```

Additionally: narrow `_STORAGE_CONTEXTS` from all 111 classes to the ~10 most
common (CT/MR/XA/US/MG/PT/DX/SR/OT/ECG), and make `deliver()` translate a
`ValueError` from negotiation into a `DeliveryResult` with an actionable
error string so the operator sees *"association negotiation failed: too many
SOP classes (N) for the 128-context limit"* instead of `ValueError`.

## H-2. Disk-full purge loop never terminates when a delete fails

**Location:** `src/mercure_gateway/disk.py:115-123` and `:146-152`, with
`src/mercure_gateway/spool/__init__.py:905-917`.

`check_once()` loops while usage is over threshold:

```python
while pct >= self._warning_pct:
    if not self._spool.purge_oldest_delivered():
        ...
        break
    disk = shutil.disk_usage(self._spool.spool_dir)
    pct = disk.used * 100.0 / max(1, disk.total)
```

But `purge_oldest_delivered()` returns `True` whenever a SENT row exists —
including when `_purge_study_dir()` bails out on `OSError` **without deleting
the row** (intentional per its docstring at `923-943`: *"a later purge pass can
retry"*):

```python
row = self._db.list_oldest_delivered()
if row is None:
    return False
self._purge_study_dir(raw_uid=str(row["study_uid"]), study_id=int(row["id"]))
return True          # <- "True" even when nothing was deleted
```

So on read-only / full / locked media the loop re-selects the same study,
fails the same `rmtree`, returns `True`, and spins forever — with **no sleep**,
pegging a CPU and hammering an already-failing disk, in exactly the failure
condition the monitor exists to protect against. `_enforce_spool_cap` has the
identical shape.

This is a control-flow correctness bug born from an ambiguous return
contract: the boolean means both "nothing eligible" and "made progress."

**Fix.** Make the return value mean *progress*, and break on no progress:

```python
# spool/__init__.py
def purge_oldest_delivered(self) -> bool:
    row = self._db.list_oldest_delivered()
    if row is None:
        return False
    study_id = int(row["id"])
    self._purge_study_dir(raw_uid=str(row["study_uid"]), study_id=study_id)
    # _purge_study_dir deliberately leaves the row in place when the delete
    # fails so the study stays visible; that is NOT progress.
    return self._db.get_study(study_id) is None
```

Keep `_purge_study_dir`'s row-retention behaviour (it is correct and preserves
US-04). Optionally add a bounded retry count / sleep in the `disk.py` loops as
defence in depth.

## H-3. Report C-STORE SCP thread and port leaked when the associate call raises

**Location:** `src/mercure_gateway/reports/move.py:112-144`.

```python
server = store_ae.start_server(("", self.store_scp_port), ..., block=False)  # 112
if server is None: raise ...                                                 # 117-118
ae = AE(...)
assoc = ae.associate(self.host, self.port, ae_title=self.aet)                 # 123  <- unguarded
if not assoc.is_established:
    server.shutdown(); raise ...                                             # 124-128
try: ... finally:
    assoc.release(); server.shutdown()                                       # 142-144
```

`ae.associate()` raises (`socket.gaierror`, `OSError`, pynetdicom `RuntimeError`)
on any transient network failure — the exact case C-MOVE retry exists to
cover. Because the `try/finally` starts at line `130`, the store-SCP reactor
thread and its bound port are never released. The very next `retrieve()` then
gets `server is None` from the failed bind and raises
`ReportRetrieveError` permanently. **One transient DNS hiccup disables all
report retrieval until the process is restarted** — a latent availability bug
that will present as a confusing permanent failure in the field.

**Fix.** Extend the guarded region to cover the server, not just the
association:

```python
server = store_ae.start_server(("", self.store_scp_port),
                               evt_handlers=[(evt.EVT_C_STORE, on_c_store)],
                               block=False)
if server is None:
    raise ReportRetrieveError("failed to start the gateway C-STORE SCP")
try:
    assoc = ae.associate(self.host, self.port, ae_title=self.aet)
    if not assoc.is_established:
        raise ReportRetrieveError(
            f"C-MOVE association rejected by PACS at {self.host}:{self.port}"
        )
    try:
        for match in matches:
            ...  # send_c_move loop
        with received_lock:
            stored = dict(received)
    finally:
        assoc.release()
finally:
    server.shutdown()
```

The nested `try/finally` is the point: it keeps the existing
association-level cleanup while guaranteeing the SCP always shuts down.

## H-4. State-machine writes inside `_dispatch` are unguarded — a DB error strands a delivered study

**Location:** `src/mercure_gateway/forwarder/__init__.py:229-248`.

Only `handler.deliver()` is wrapped (`222-227`). The bookkeeping calls sit
outside it:

```python
if result.ok:
    self.spool.complete(task.study_id, task.target_name)   # 230 — can raise
    self._audit(FORWARD_COMPLETE, detail)
    return
self._fail_task(task, result.error or "delivery failed")   # 234 — can raise
...
self.spool.reforward(task.study_id, task.target_name)      # 248 — can raise
```

If any of these raises (`SQLITE_FULL` on commit — plausible, since it tends to
happen precisely while the disk monitor is purging; or a `KeyError` for a
study the retention purge already removed), the exception escapes `_dispatch`
→ `process_once` → is caught only by the worker loop's catch-all. The route
stays `sending`, and **nothing ever re-claims a `sending` route**
(`spool/db.py:737-782` selects `status = 'waiting'` only). Delivery already
happened on the wire but was never recorded: the DB says the study is in
flight forever, until an *unclean* restart happens to run `recovery.py` (a
clean restart writes the marker, so recovery is skipped).

This is the most consequential error-handling defect in the forwarder,
because it silently corrupts the queue's source of truth.

**Fix.** Degrade a bookkeeping failure to a retryable route error instead of
letting it escape:

```python
if result.ok:
    try:
        self.spool.complete(task.study_id, task.target_name)
    except Exception:  # noqa: BLE001 — delivery already succeeded
        logger.exception(
            "delivery succeeded but completion recording failed for "
            "study %d target %s; leaving route errored for retry",
            task.study_id, task.target_name,
        )
        self._fail_task(task, "delivery ok; completion recording failed")
        return
    self._audit(FORWARD_COMPLETE, detail)
    return
```

Also apply the same guard to `_fail_task` (a `spool.fail()` failure must not
mask the original delivery error) and consider having `Forwarder.start()`
reset routes left in `sending` whose study is `ERROR`, since the
`stop()`-abandons-route path (`151-165`) has the same stranding profile.

## H-5. `postJson` discards every failure detail, so PHI operations fail silently

**Location:** `web/src/api.ts:131-135` (and its callers
`retryStudy` / `enqueueStudy` / `requestReport` / `refreshReport` /
`postServiceAction`).

```typescript
export async function postJson(url: string): Promise<{ status: string } | null> {
  const res = await apiFetch(url, { method: "POST" });
  if (!res.ok) return null;
  return (await res.json()) as { status: string };
}
```

A 400 with `"detail": "study is not in an error state"` and a 500 with a
backtrace both collapse to `null` → `false` in the caller. The panel renders
"Retry failed" with no reason. Compare `saveConfig` (`220-231`), which
*does* extract `detail` — so the codebase already knows the pattern and just
does not apply it here. On a medical gateway, an operator who cannot tell
"already retried" from "gateway down" from "PACS rejected" is one
misdiagnosed incident away from a patient-data delay.

The same file also holds a module-scope `restartRequired` flag plus a
`restartListeners` Set (`198-237`) — a mutable store living outside React's
tree. It works, and the `App.tsx` consumer (`165-166`, `186-188`) is correct,
but it is an implicit global that a new page would have to know about; a
`useRestartRequired()` hook over a small context would make the dependency
explicit.

**Fix.**

```typescript
export async function postJson<T = { status: string }>(url: string): Promise<T> {
  const res = await apiFetch(url, { method: "POST" });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    throw new Error(detail?.detail ?? `request failed (HTTP ${res.status})`);
  }
  return (await res.json()) as T;
}
```

Callers then surface the message (`setError(err.message)`) instead of a bare
boolean. If a boolean return is needed for a specific caller, make it an
explicit `try/catch` at that call site rather than discarding the detail
library-wide.

## H-6. Echo probe has no error boundary — stuck "probing" badge plus an unhandled rejection

**Location:** `web/src/pages/DestinationsView.tsx:263-273` (`handleEcho`),
with the missing style at `:483`.

```typescript
const handleEcho = async (d: Destination) => {
  const key = probeSignature(d);
  setEchoState((prev) => ({ ...prev, [key]: "probing" }));
  const status = await echoProbe(...);          // throws on a network error
  setEchoState((prev) => ({ ...prev, [key]: status }));
};
```

`echoProbe` (`149-172`) carefully handles non-ok responses (including a
well-reasoned 401 → `"expired"` mapping), but a raw `fetch` **rejection**
— backend down, DNS, CSP — propagates out of `handleEcho`, which has no
`try/catch`. Two visible symptoms: the badge is stuck on "probing" forever
(the state was set, the follow-up never ran), and the promise rejects
unhandled. The button is also `disabled` while "probing"
(`:477`), so the operator cannot even retry.

Related, at `:483`: the badge class for the `"expired"` case is `"amber"`, but
`web/src/index.css` defines `.badge.yellow`, `.badge.green`, `.badge.red`,
`.badge.gray`, `.badge.blue` — **there is no `.badge.amber` rule**. The
"expired" badge renders unstyled, which is doubly unfortunate because that is
the one state the code specifically engineered to be honest
(`echoProbe`'s 401 comment at `:158-164` explains why "expired" beats "error").
The token exists as `--yellow` (`index.css:28, 66`) but is never wired to the
class the component uses — a naming drift between the CSS vocabulary and the
component vocabulary.

**Fix.**

```typescript
const handleEcho = async (d: Destination) => {
  const key = probeSignature(d);
  setEchoState((prev) => ({ ...prev, [key]: "probing" }));
  try {
    const status = await echoProbe(
      String(d.host), Number(d.port),
      String(d.aet_target ?? ""), String(d.aet_source ?? ""),
    );
    setEchoState((prev) => ({ ...prev, [key]: status }));
  } catch (err) {
    setEchoState((prev) => ({ ...prev, [key]: "error" }));
    setError(err instanceof Error ? err.message : "probe failed");
  }
};
```

And in `index.css`, either rename the component's `amber` to `yellow` or add:

```css
.badge.amber { background: var(--yellow-dim); color: var(--yellow); }
```

Pick one badge-colour vocabulary and keep the component and stylesheet on it;
the safest fix is renaming the class to match the existing token.

## H-7. Tamper-evidence is built but never automatically verified

**Location:** `src/mercure_gateway/audit/anchoring.py:242`
(`verify_anchor_signatures`) and `src/mercure_gateway/audit/__init__.py:278`
(`AuditLog.verify`), reached only from `web/routes.py:1044-1050`.

The chain, the external head-anchor file, and the hub Ed25519 signed anchors
(ADR-0006) are all implemented and tested — but verification is **only**
reachable on demand via `GET /api/audit/verify`, which `routes.py:248-250`
deliberately keeps out of the status probe (correctly, citing a DoS vector at
15 s intervals). `verify_anchor_signatures` is called **only from tests**
(`tests/test_hub_bookkeeper_interop.py`, `tests/test_audit_anchor_signing.py`)
— grep finds no production caller. Nothing verifies the anchor file against
the DB head at boot, and `prune()` (`audit/__init__.py:385-459`) deletes
events and re-anchors the chain without first asserting the pre-prune chain
was intact.

For a tamper-evidence control, "verify only when someone remembers to click"
is close to "no control": the whole value is detecting a forged chain before
the forger gets to prune it. `prune` in particular records `head_before` /
`head_after` in a `PRUNE_AUDIT` event *so the rewrite is verifiable against an
externally anchored head* — but nothing ever performs that comparison. The
machinery and its documentation are excellent; the wiring is what is missing.

**Fix.**

1. Verify on startup, once, cheaply — the head anchor comparison is O(1), not
   the O(n) full-chain walk:

```python
# main.py, after audit/anchorer wiring, before the pipeline starts
try:
    audit.assert_anchor_intact()   # compares DB head to the anchor file head
except ChainError as exc:
    logger.error("AUDIT CHAIN MISMATCH at boot: %s — refusing to start", exc)
    return 1                       # main() currently never returns non-zero
```

   Refuse to boot on a mismatch (and surface it in `BackendDownView`), which is
   the fail-closed behaviour the rest of the design assumes.

2. In `prune()`, verify before deleting, and refuse to prune a chain that does
   not already verify:

```python
ok, errors = self.verify()
if not ok:
    raise ChainError(f"refusing to prune a chain that does not verify: {errors}")
```

3. Expose `verify_anchor_signatures` through `AuditLog` and call it from the
   same boot check when `hub_reporting.anchor_public_key` is configured, so
   the hub-signed anchors are actually checked in production rather than only
   in interop tests.

## H-8. Session cookie is missing the `Secure` flag

**Location:** `src/mercure_gateway/web/auth.py:121-127`.

```python
response.set_cookie(
    _COOKIE_NAME, token, max_age=_SESSION_TTL_SEC,
    httponly=True, samesite="lax",
)
```

`httponly` and `samesite="lax"` are right, but `secure` is absent. The
appliance is served over plain HTTP to `127.0.0.1:8080` from the Tauri shell,
so setting `secure=True` unconditionally would break the local case — but the
code can tell the difference: `request.url.scheme` (or whether the bind is
loopback). Today, if an operator puts the panel behind a TLS-terminating proxy
on the network — which `_enforce_bind_security` explicitly permits as a
"deliberate deployment" — the session token travels in cleartext on the first
hop and is replayable by anyone on the path. This is the auth half of the
C-3 posture: the boot check guards *who can reach* the API, the cookie guards
*the token itself*.

**Fix.**

```python
response.set_cookie(
    _COOKIE_NAME,
    token,
    max_age=_SESSION_TTL_SEC,
    httponly=True,
    samesite="lax",
    secure=request.url.scheme == "https",
)
```

(Also note `_session_secret` at `:85` falls back to an ephemeral random secret
when no password hash is configured — that is correct for the auth-off case,
but it means every restart invalidates cookies; fine, worth a code comment so
a reader does not "fix" it into a constant.)

---

# Medium

## M-1. The 128-context forwarder bug and the C-1/C-2 traversal share one root cause: duplicated "untrusted UID / budget" logic

`reports/dicomweb.py`, `reports/move.py`, `spool/__init__.py`, and
`forwarder/handlers/dicom.py` each re-implement a policy that should live in
one place — "a UID from the wire is untrusted" and "DIMSE has a 128-context
ceiling." C-1, C-2, and H-1 are three separate instances of the same missing
shared boundary. Fixing them individually will produce a fourth copy. Extract
`validate_uid` into a `mercure_gateway.uids` module, and put the context
budget in `sop_classes.py` next to the `assert len(STORAGE_SOP_CLASSES) <=
_MAX_CONTEXTS` that already exists at `:303`.

## M-2. Every delivery handler opens with the same 16-line boilerplate

`forwarder/handlers/dicom.py:79-95`, `sftp.py:61-74`, `folder.py:36-49`,
`s3.py:25-37` each repeat: study_uid lookup → `study_files` → empty-list
error. Three of the four also take a `spool_dir: Path` parameter they never
use (they use `self.spool`) — an unused protocol parameter is both a smell and
a live hazard: the next handler to be written may use `spool_dir` and silently
read a different tree than the one `self.spool` owns.

**Fix.** Hoist into the base/protocol level:

```python
# forwarder/handlers/__init__.py (or BaseStudyHandler)
def study_files_or_error(spool: Spool, task: ClaimedTask) -> list[Path] | DeliveryResult:
    try:
        uid = spool.study_uid(task.study_id)
    except KeyError:
        return DeliveryResult(ok=False, error="study not found")
    files = spool.study_files(uid)
    if not files:
        return DeliveryResult(ok=False, error="no DICOM files found for study")
    return files
```

Then drop the unused `spool_dir` parameter from the protocol, or actually use
it — the choice should be deliberate.

## M-3. `main()` is a 193-line, cc-20 composition function

`src/mercure_gateway/main.py:422-614`. It does argument parsing, config load,
env overrides, USB detection, bind security, update check, DB open, audit +
anchorer wiring, hub start, text log, spool, retention purge, recovery,
receiver/forwarder/report-retriever construction, service backend, tray, and
the signal-handler/shutdown dance. Highest-cyclomatic function in the backend
(measured; see the table in Appendix A). It is readable because it is linear
and well-commented, but it mixes *deciding* with *constructing*, which makes
both testing and startup-order reasoning hard — and the one real bug in it
(`exit_code` is never set non-zero, `594-612`, so a boot failure can exit 0)
is exactly the kind a refactor into named steps would surface.

**Fix.** Extract linear sections into functions returning what they build:

```python
def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(...)
    if args.write_default_config:
        return _write_default_config(args.config)

    config = _load_or_init_config(args)
    _apply_runtime_profile(config)          # env overrides + USB detection
    _enforce_bind_security(config)
    _check_for_updates(config)

    with _build_components(config) as comps:      # contextmanager: DB, audit, hub
        return _run(comps, args)                   # blocking; returns exit code
```

Each `_build_*` is independently unit-testable and the shutdown/cleanup
becomes a `with` block instead of a hand-written `finally` (`594-612`).

## M-4. `_restore_redacted_secrets` is a cc-20 name-matching heuristic

`src/mercure_gateway/web/routes.py:705-766`, the second-highest complexity
function in the backend. It restores `***` sentinels by destination name and
then by positional fallback for renamed destinations. Positional matching of
secrets across a renamed list is a correctness risk on a config screen whose
primary use case is *editing destinations* — if an operator renames a
destination and reorders the list in the same save, the positional branch can
hand destination B's credential to destination A and persist it. There is no
test covering rename-plus-reorder, and the failure is silent (the wrong value
saves and the panel shows `***`).

**Fix.** Give destinations a stable client-side identity that survives renames
(see how `DestinationsView` already does this with `probeSignature`/stable ids
and explicitly strips them before payload at `handleSave`) and key the restore
on that id instead of on list position. If positional fallback must stay as a
last resort, log a warning when it fires so the operator can confirm which
credential landed where. Add a test for rename + reorder.

## M-5. Tray state is implemented twice, in two languages, and they disagree

`src/mercure_gateway/tray.py:25` (`derive_tray_state`, 4 states including
`removable`/`usb_mode`) vs `src-tauri/src/lib.rs:115-138` (`derive_state`, 3
states). The Rust copy **drops** the `removable` state, so a USB-mode
appliance shows the wrong icon. The Python copy is referenced only by tests
(grep finds no `src/` caller) — so the *live* implementation is the one that
diverged. Two more issues in the Rust poller: an inner queue/stats fetch
failure (`:124-135`) silently falls through to `TRAY_IDLE` (a USB-ejected or
errored gateway looks idle), and the `Arc<AtomicU8>` at `:177`/`:300` is
write-only — written every poll, never read. A new `Client` is also built per
poll when one could be reused.

Cross-language reimplementation of the same state machine is the classic
drift trap; here it has already drifted.

**Fix.** Make Python the single source of truth and Rust a thin renderer:
have the gateway expose `GET /api/tray-state` returning the derived state
enum, and have `lib.rs` fetch + match on strings instead of recomputing. Then
either delete `tray.py:derive_tray_state` (if the endpoint replaces it) or
keep it and have the endpoint call it, so there is one implementation. Also:
propagate the fetch failure as a distinct "unknown/degraded" icon rather than
`TRAY_IDLE`, and drop or read the `AtomicU8`.

## M-6. Recovery-created studies are never enqueued; errored routes strand after a clean restart

`src/mercure_gateway/recovery.py:139-157` + `main.py:492-521` +
`spool/__init__.py:444-493`.

- Recovery re-registers orphaned files via `spool.receive()`, but only
  `store_instance()` arms the auto-enqueue timer (`:369`), so recovered
  studies sit in `RECEIVED` forever unless an operator finds them in the
  console. `main.py` starts the components right after `recover()` with no
  enqueue pass.
- `_auto_enqueue()` swallows all exceptions by design (`:490-493`), but the
  timer already fired — a study whose enqueue failed once is never retried.
- `Forwarder.stop()` (`forwarder/__init__.py:151-165`) deliberately leaves
  in-flight routes in `error`. On the next boot the clean-shutdown marker
  exists → `recover()` is skipped → nothing re-claims `error` routes
  (`db.py:737` selects `waiting` only). Documented as "manual re-forward can
  resume," but in practice those studies are invisible until someone opens the
  console.

**Fix.** One enrollment pass after `recover()` in `main.py`:

```python
for row in spool.list_studies_with_route_counts(state=StudyState.RECEIVED.value):
    if not spool.get_routes(int(row["id"])):
        try:
            spool.enqueue_study(int(row["id"]))
        except ValueError as exc:
            logger.warning("post-recovery enqueue of study %s failed: %s",
                           row["study_uid"], exc)
```

And give `Forwarder.start()` responsibility for resetting stale errored
routes (`error → waiting`) when the study is still `ERROR`, since that is the
only component that knows whether a retry is wanted.

## M-7. SFTP `connect()` has no timeout — a hung server pins a worker and its route

`src/mercure_gateway/forwarder/handlers/sftp.py:100-115`.

```python
client.connect(hostname=..., port=..., username=..., pkey=key)   # or password=
```

paramiko's default `timeout=None` waits indefinitely. `Forwarder.stop()`'s
`join_timeout` abandons the thread (documented), but the route stays
`sending`, and the H-4 stranding profile applies. Note also that
`SSHClient()` with no credentials will silently try the SSH agent and
`~/.ssh` keys — surprising on an appliance, and a path by which a user-level
key on the appliance becomes a forwarding credential.

**Fix.**

```python
client.connect(
    hostname=..., port=..., username=..., pkey=key,
    timeout=self.destination.timeout_sec,     # add to SFTPDestination
    banner_timeout=10, auth_timeout=10,
    look_for_keys=False, allow_agent=False,
)
```

## M-8. C-MOVE is issued once per matched instance, each moving the whole study

`src/mercure_gateway/reports/move.py:130-138` with `_move_dataset` at
`161-168`.

The query is STUDY-level (`QueryRetrieveLevel="STUDY"`, only
`StudyInstanceUID`), but the loop iterates every `ReportMatch` from an
IMAGE-level C-FIND. A study with 40 SR instances triggers 40 identical
full-study C-MOVEs; the PACS pushes the whole study 40 times while `saved`
records only the matched instances and the rest are discarded. This is
unnecessary load on a clinical PACS — exactly the kind of thing that gets a
gateway blocked by a hospital's PACS admin.

**Fix.** Dedupe by study:

```python
for study_uid in {m.study_uid for m in matches}:
    ds = Dataset()
    ds.QueryRetrieveLevel = "STUDY"
    ds.StudyInstanceUID = study_uid
    for status, _ in assoc.send_c_move(ds, self.store_scp_ae_title, _STUDY_ROOT_MOVE):
        ...
```

(Series/instance-level move would be more precise if the PACS supports it;
study-level dedupe is the minimal correct fix.)

## M-9. C-STORE results snapshotted before sub-operations are guaranteed complete

`src/mercure_gateway/reports/move.py:139-141`.

```python
with received_lock:
    stored = dict(received)      # immediately after the MOVE status loop
```

The PACS delivers instances on a *separate* association to the store SCP. A
PACS that sends the final MOVE success before finishing its C-STORE
sub-operations (or a slow last instance) yields `stored.get(uid) is None` →
the report is marked failed even though the instance arrives a moment later.
Retry eventually succeeds, so the audit chain records a spurious failure.

**Fix.** Wait for the store SCP to go quiet before snapshotting — e.g. a
bounded wait on a `threading.Event` set by `on_c_store` when no instance has
arrived for N ms (with a short overall deadline), or an explicit idle check,
before building `stored`.

## M-10. No frontend test job in CI, and the new a11y scan is not exercised there

`.github/workflows/ci.yml` has no vitest job; the only web step is
`npm run build` inside `build-spa`. The frontend has 11 test files / 65 tests
— including the new untracked `web/src/test/a11y-scan.test.tsx` — and none of
them run in CI. A regression in the SPA can merge green as long as it
type-checks and bundles. This matters more here than usual because the
a11y scan is the only automated guard for WCAG conformance on a medical
admin panel, and it is currently untracked and unrun.

Also: the scan stubs (`a11y-scan.test.tsx:82-153`) never render the
interactive `<tr>` row markup (each view's stub returns one flat item), so
the table-row ARIA paths — the part most likely to break, since ARIA
restricts what may go in a `<tr>` — are unscanned. The test file's own header
comment is honest about the jsdom limitation (no layout → no contrast or
reflow checks), which is good practice; extend that honesty to the row
coverage gap.

**Fix.** Add a `web-test` job:

```yaml
  web-test:
    runs-on: ubuntu-latest
    defaults: { run: { working-directory: web } }
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with: { node-version: "24", cache: npm, cache-dependency-path: web/package.json }
      - run: npm ci
      - run: npm run lint
      - run: npm test -- --coverage
      - run: npx tsc -b      # project-references build, not --noEmit
```

Then add stubs with multiple items per view so the `<tbody>` row path is
actually scanned.

## M-11. Duplicated 1.3 MB `axe.js` assets, referenced by nothing

`web/public/axe.js` and `src/mercure_gateway/web/static/axe.js` — both
untracked, both 1,305,279 bytes, byte-identical (verified with `cmp`). Nothing
references either: the test file imports `axe-core` from `node_modules`.
One copy is in the directory the FastAPI app serves as static assets and
rides into the built SPA / installer payload; the other is in the public dir
that Vercel-style hosts serve verbatim. Two copies of a 1.3 MB accessibility
library that no production code loads is dead weight in a shipped artifact.

**Fix.** Delete both. If a browser-based (non-jsdom) scan is planned later,
import from `node_modules` as the test already does, or add one copy under
`web/public/` and reference it explicitly from a script tag.

## M-12. Weak default password hashing, and `bcrypt` is not a declared dependency

`src/mercure_gateway/web/auth.py:61-80`.

`verify_password` tries `bcrypt` and falls back to a single-round
`hashlib.sha256(salt + password)` on `ImportError`. But `bcrypt` does not
appear in the project dependencies, so **the default path is the fallback**:
a single SHA-256 pass over a salted password is brute-forceable at billions
of attempts per second on commodity GPU. The field label in
`config/__init__.py:394-407` says `"Bcrypt hash of the web UI password"`,
which is no longer the guaranteed construction. Also, `verify_password`
catches only `ImportError` — a malformed stored hash raises `ValueError` from
bcrypt and becomes an HTTP 500 instead of a clean 401.

**Fix.** Add `bcrypt` to the project dependencies (it is already the intended
path) and drop the sha256 fallback, or keep the fallback but name it honestly
in the field description and accept `sha256$salt$hash` input. And:

```python
except (ImportError, ValueError):
    # a malformed stored hash is a config problem, not a server fault
    return False
```

## M-13. Backend error handling is inconsistent for the same "parse JSON detail" operation

`src/mercure_gateway/web/routes.py` does the same `json.loads(detail)` three
ways:

- `study_timeline` (`686-687`) — `with contextlib.suppress(TypeError, ValueError)`
- `export_audit` (`1076`) — bare `json.loads(ev.get("detail") or "{}")`
- `diagnostics_export` (`1205`) — bare `json.loads(row["detail"])`

The schema defaults detail to `'{}'` so these do not fail today, but the two
unguarded calls turn a malformed row into an HTTP 500 where the sibling
endpoint degrades to a missing field. Same operation, three contracts.

**Fix.** Extract one helper used by all three:

```python
def _detail_json(row: Mapping[str, Any]) -> dict[str, Any]:
    """Parse an audit detail blob, tolerating malformed/missing values."""
    try:
        return json.loads(row.get("detail") or "{}")
    except (TypeError, ValueError):
        return {}
```

## M-14. `get_report_content` repeats its response dict four times

`src/mercure_gateway/web/routes.py:966-1025`. Four branches each assemble a
near-identical `{content_type, filename, content}` response with only the
media type and bytes differing. Low risk, but it is the exact shape a future
branch will copy-paste with one field wrong.

**Fix.** One builder:

```python
def _report_response(content: bytes, media_type: str, filename: str):
    return Response(content=content, media_type=media_type,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})
```

---

# Low

## L-1. Dead code and dead parameters

- `src/mercure_gateway/hotplug.py:192` — `self._device_path =
  self._resolve_device_path()` is assigned and never read (grep-confirmed);
  `_resolve_device_path` (`221-249`) stats every mount in `/proc/mounts` at
  construction for nothing.
- `src/mercure_gateway/service_backend.py:30-40` — `_service_exe_args`:
  both branches of `if getattr(sys, "frozen", False)` return `"--web"`. Dead
  branch, and its docstring describes a distinction the code does not make.
- `src/mercure_gateway/tray.py:25` — `derive_tray_state` (4 states) is
  referenced only by tests; see M-5.
- `src/mercure_gateway/reports/transport.py:164-168` — `_StubFactory` is a
  test double shipped inside a production module.
- `src/mercure_gateway/reports/__init__.py:117-120` — `_loop_iteration` is
  unused (the poller uses `_poll_loop`).
- `src/mercure_gateway/spool/__init__.py:161` — `StudyState.RECEIVING` is in
  the enum and in recovery's `non_terminal` set but is never written by any
  code path; it dead-weights the state machine and misleads readers of the
  recovery table.
- `web/src/ui/flow.tsx:62` — `<circle cx={cx} cy={0} r={0} />` renders
  nothing; leftover scaffolding.
- `web/src/pages/ConfigView.tsx:8` — `const [, setConfig] = useState(...)`
  is write-only state (the value is never read).
- `forwarder/handlers/{sftp,folder,s3}.py` — `deliver(task, spool_dir)` takes
  `spool_dir` and never uses it; see M-2.
- `src/mercure_gateway/main.py:404` — `text_log._path` reaches into a private
  attribute; expose a property.
- `src/mercure_gateway/main.py:594-612` — `exit_code` is assigned `0` and
  never set non-zero, so the boot-failure path can exit 0 (see M-3/H-7).

## L-2. Unreachable and string-coupled branches in the frontend

- `web/src/pages/ConfigView.tsx:62-63` — the `SyntaxError` branch is
  unreachable: the `catch` at `:42` already converts parse failures into a
  lint message before this code runs.
- `web/src/pages/ConfigView.tsx:38` — `f.message.startsWith("Invalid JSON")`
  couples the component to the exact string emitted at
  `web/src/config/lint.ts:57`. Change that message and the error styling
  silently degrades. Export a sentinel or a typed result from `lint.ts`
  instead of matching on prose.
- `web/src/pages/ConfigView.tsx:98,101` — `msg.startsWith("Config")` decides
  ok-vs-error CSS class *and* the ARIA role from message text. An
  operator-visible classification and an accessibility attribute should not
  hang on an English prefix.
- `web/src/App.tsx:31` — redeclares the `QueueStats` shape inline rather than
  reusing the type from `api.ts`; a drift waiting to happen.
- `web/src/App.tsx:34-38` — three `.catch(() => setX(null))` handlers
  conflate "fetch failed" with "no data," so a dead backend renders the
  dashboard as empty numbers instead of an error state. (The shell-level
  `backendUnreachable` branch in `AuthContext` does catch this — but only for
  the auth probe, not for these three fetches.)
- `web/src/ui/flow.tsx:19-20` — `dot: string` should be the documented union
  `"green" | "gray" | "yellow" | "red" | "accent"`; the JSDoc already
  describes it, the type just does not enforce it.
- `web/src/context/AuthContext.tsx` — `checkAuth` is unmemoized and called
  from `useEffect(..., [])` (exhaustive-deps warning class); no
  `AbortController`, so a fast logout/login can resolve a stale request.

## L-3. `enqueue_study` dead computation

`src/mercure_gateway/spool/__init__.py:783-787`:

```python
if self._db.get_routes(study_id):
    return 0                       # already routed
before = len(self._db.get_routes(study_id))   # provably always 0
```

`before` is always 0 by the early return above it; the `created` arithmetic is
really just `len(get_routes)` after the enqueue. Remove `before`.

## L-4. Unknown `report_type` silently disables SOP-class filtering

`src/mercure_gateway/reports/find.py:95` and `reports/dicomweb.py:86-88`:

```python
wanted_sops = {_REPORT_SOP_CLASSES[t] for t in report_types if t in _REPORT_SOP_CLASSES}
... if wanted_sops and sop_class not in wanted_sops: continue
```

An unrecognized `report_types` entry yields an empty set, and the
`and wanted_sops` guard turns the filter into a no-op that returns **every**
instance instead of none. The web layer validates (`web/routes.py:902`), so
this is defence in depth — but the silent "match everything" failure mode is
the wrong default. Raise `ValueError(f"unknown report_type {t!r}")`.

## L-5. Unknown SOP classes bucketed as `pdf`

`reports/move.py:172` and `reports/dicomweb.py:179`:
`sub = "sr" if sop_class == SR_SOP_CLASS else "pdf"` — a non-SR/non-PDF
instance is written under `pdf/` and `RetrievedReport.report_type` returns
`"unknown"`. Use an explicit third bucket or reject.

## L-6. Relative `Link: rel="next"` URLs break DICOMweb pagination

`src/mercure_gateway/reports/dicomweb.py:187-197`, consumed at `:100-101` /
`157-161`. `_parse_link_next` returns the URL verbatim; a server returning a
*relative* next URL (`</dicomweb/studies?offset=50>; rel="next"`) is then
handed to `_request`, which prepends `https://` → `requests` raises
`InvalidURL` → `find()` dies mid-pagination (surfaced upstream as a failed
report). Only the first page is ever seen, silently.

```python
from urllib.parse import urljoin
next_url = urljoin(url, raw_next) if raw_next else None
```

## L-7. SLA tracking is in-memory only and unbounded

`src/mercure_gateway/reports/__init__.py:83, 131-147, 176`. `_requested_at`
grows once per report id for the process lifetime and is lost on restart, so
SLA timing resets whenever the gateway restarts. Persist `requested_at` on
the `reports` row (the schema already has `retrieved_at`; a `requested_at`
column is consistent) or bound the dict.

## L-8. `STUDY_SENT` emitted twice when the last two routes complete concurrently

`src/mercure_gateway/spool/__init__.py:697-721`. Two workers each
`mark_route_sent`, both see `all_routes_complete()`, both set `SENT` and emit
`STUDY_SENT`. The writes are idempotent; the duplicated audit event is the
only visible effect, but on a tamper-evident chain a duplicate event is worth
avoiding. Make the promotion conditional and emit only on success:

```sql
UPDATE studies SET state='SENT' WHERE id=? AND state!='SENT'
```

then emit only when `cur.rowcount == 1`.

## L-9. `instance_meta` committed before the durability barrier

`src/mercure_gateway/spool/__init__.py:353-363, 566-579`.
`_apply_transfer_syntax` inserts the provenance row (best-effort, inside a
broad `except` at `577-579`) *before* `_fsync_instance` guarantees the bytes.
A crash in that window can leave a provenance row for bytes that never
landed. Low impact — provenance is derived and rebuildable — but moving the
`insert_instance_meta` call to after `_fsync_instance` makes the ordering
airtight.

## L-10. Duplicate-instance counters can drift under concurrent C-STORE

`src/mercure_gateway/spool/__init__.py:328-363`. Duplicate detection is
filesystem-based (`new_instance = not path.exists()`, `new_series` via a
separate `SELECT`), so two threads storing the same new instance can each
compute "new" and each increment `num_instances` / `num_series`
(`db.py:433-448`), against `db.upsert_study_instance`'s documented promise of
"a strict no-op for state and counters" (`db.py:411-419`). The second
`insert_instance_meta` also hits the `instance_uid` UNIQUE constraint and is
swallowed by the broad `except` at `577-579`, silently losing provenance.
With ≥25 concurrent associations (`receiver/__init__.py:59`) and modalities
that re-send after timeout, this is reachable in practice.

```python
with self._db.transaction() as conn:
    cur = conn.execute(
        "INSERT INTO instance_meta (...) VALUES (...) ON CONFLICT(instance_uid) DO NOTHING",
        (...))
    inserted = cur.rowcount == 1
    new_series = inserted and not self._db.has_series(study_uid, series_uid)
```

## L-11. `_value` is a dispatch chain that re-implements a type table

`src/mercure_gateway/reports/render.py:71-94` — cc 15 in 24 lines, an
`if value_type == ...` ladder over SR ValueType. It works, but every new
ValueType adds a branch, and the `DATETIME/DATE/TIME` case already hides a
sub-dispatch inside a dict literal. A table is shorter and self-documenting:

```python
_VALUE_GETTERS: dict[str, Callable[[Any], str]] = {
    "TEXT":    lambda i: str(getattr(i, "TextValue", "") or ""),
    "PNAME":   lambda i: str(getattr(i, "PersonName", "") or ""),
    "UIDREF":  lambda i: str(getattr(i, "UID", "") or ""),
    "NUM":     _numeric_value, "NUMERIC": _numeric_value,
    "CODE":    _code_value,
    "DATETIME": _tag_value("DateTime"), "DATE": _tag_value("Date"),
    "TIME":    _tag_value("Time"),
}
return _VALUE_GETTERS.get(value_type, lambda i: "")(item)
```

## L-12. Redaction-sentinel round-trip depends on field naming heuristics

`src/mercure_gateway/web/routes.py:705-766` — see M-4. Separately worth
noting: `config/__init__.py` marks secret fields, `redact.py` turns them into
`***`, and `_restore_redacted_secrets` un-turns them by name. The chain is
correct but the contract is implicit — a new secret field added to the model
is silently *not* redacted until someone adds it to the marker list. Consider
deriving the secret-field set from the model declaration (a
`Field(json_schema_extra={"secret": True})` marker read by both `redact.py`
and the restore path) so the two halves cannot drift.

## L-13. `textlog` rotation has a TOCTOU across concurrent threads

`src/mercure_gateway/textlog.py` — `_write` checks the size and rotates
outside the write lock (or re-stats between check and move, depending on
path). Concurrent writers can both rotate, or rotate while another thread is
mid-write. Low impact (operations log, not PHI-bearing), and rotation during
concurrent writes is rare; holding the lock across the rotate is the fix if
the log is ever relied on for forensics.

---

# Findings verified as *not* bugs (excluded)

Recorded so the next reviewer does not re-litigate them:

1. **`DestinationsView.typeWarnings` prefix matching** — the actual code is
   `w.path.startsWith(\`destinations[${i}].\`)` (`:305-306`). The trailing `.`
   separator makes `destinations[1].` unable to match `destinations[10].foo`.
   A plausible-looking bug; not one.
2. **`phi_scope` not applied at audit-write time** — deliberate and correct.
   `redact_phi` is applied at *every* read path (`audit/__init__.py:348-355`,
   `routes.py:1069-1077`, `routes.py:1200-1205`); storing the full detail is
   what keeps the chain hash verifiable against the externally anchored head.
3. **`insert_route` ON CONFLICT** — `db.py:650-658` uses
   `ON CONFLICT(study_id, target_name) DO NOTHING`, so the
   `_requeue_complete_routes` + `_auto_enqueue` re-open path does **not**
   create duplicate routes. It looks like a bug from outside and isn't.
4. **Broad `except` boundaries** at `forwarder/__init__.py:222`,
   `reports/__init__.py:215`, `disk.py:96`, `receiver/__init__.py:127`,
   `sftp.py:123`, `audit/__init__.py:195` — all justified handler-boundary
   catches that map to result objects; none swallow errors silently.
5. **`Spool.store_instance` durability ordering** — fsync of file + new
   directory entries before the RECEIVED row, `synchronous=FULL` in WAL, and
   the `_dirs_to_sync` ancestor logic are sound. UID validation on the
   receive path genuinely closes the C-STORE traversal
   (`spool/__init__.py:62-70, 321-323`); `recovery.py:64-73` re-validates
   during the disk walk.
6. **Retry/backoff in `Forwarder._dispatch`** — sleep-before-requeue
   (`244-248`) is the right order; `reforward` + `claim_route(route_id)`
   cannot drift onto a different route; the attempt counter is consistent
   with `should_retry`/`fail`.
7. **Built SPA no longer in VCS** — `git ls-files
   src/mercure_gateway/web/static` returns only `favicon.svg` (M9 from the
   2026-09-08 review is fixed).

---

# Appendix A — measured complexity hotspots

Longest / most complex functions in the backend (AST walk, McCabe
approximation):

| Lines | CC | Location | Function |
|---|---|---|---|
| 193 | 20 | `main.py:422` | `main` |
| 122 | 11 | `web/routes.py:236` | `system_metrics` |
| 115 | 15 | `recovery.py:90` | `recover` |
| 77 | 6 | `web/pipeline.py:113` | `pipeline_snapshot` |
| 75 | 6 | `audit/__init__.py:385` | `prune` |
| 72 | 12 | `reports/move.py:88` | `retrieve` |
| 68 | 11 | `main.py:151` | `_build_forwarder` |
| 68 | 10 | `forwarder/handlers/sftp.py:61` | `deliver` |
| 63 | 14 | `reports/find.py:79` | `find` |
| 62 | 20 | `web/routes.py:705` | `_restore_redacted_secrets` |
| 61 | 11 | `forwarder/__init__.py:192` | `_dispatch` |
| 59 | 9 | `web/echo.py:26` | `echo_destination` |
| 59 | 4 | `web/routes.py:967` | `get_report_content` |
| 52 | 16 | `reports/dicomweb.py:70` | `find` |
| 48 | 14 | `config/encryption.py:140` | `decrypt_config_from_storage` |
| 24 | 15 | `reports/render.py:71` | `_value` |

Only `main` and `_restore_redacted_secrets` exceed a cc of 20 / length of 60
by a wide margin, which is a good sign for overall maintainability — the
quality problem here is concentrated in a handful of integration seams
(reports persistence, config round-trip, composition root), not spread across
the codebase.

---

# Appendix B — theme summary

Five recurring root causes account for most of the findings:

1. **Untrusted-UID logic is not a shared boundary.** C-1, C-2, M-1. Fix once,
   in one module, and import it everywhere.
2. **Ambiguous return contracts.** H-2 (`True` = eligible OR progress), H-5
   (`null` = failed OR nothing), M-9 (snapshot before completion). The
   booleans mean two things; callers can only guess which.
3. **Cleanup regions that start too late.** H-3 (SCP leak), H-4 (un-guarded
   bookkeeping). Every `try/finally` should begin at the resource acquisition,
   not at the step after it.
4. **Security invariants checked at boot but not at the runtime mutation
   point.** C-3, H-8, M-4. `update_config` swaps the live config object that
   auth reads, but nothing re-runs the boot check.
5. **Cross-language / cross-copy duplication with drift.** M-5 (tray state in
   Python and Rust), M-2 (four handler boilerplates), M-11 (two identical
   1.3 MB assets), M-14 (four response dicts). Each pair was correct when
   written; each has already diverged.

---

*Prepared from a full working-tree read of the backend (`src/mercure_gateway`,
~11,700 LoC), the frontend (`web/src`, ~6,900 LoC), the Tauri shell
(`src-tauri`), and CI/docs. Every finding above was verified by reading the
cited lines; agent-reported claims that did not survive verification are
listed in the "not bugs" section.*
