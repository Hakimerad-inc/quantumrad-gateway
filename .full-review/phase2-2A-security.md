# Phase 2 — Security Audit: dicom-gateway (v1.1.0-rc3, commit `aab94b5`)

**Scope:** full-stack security audit of the Python/FastAPI backend, React/TS admin
panel, and Tauri desktop shell of a DICOM store-and-forward gateway handling PHI.
**Method:** every finding below was verified against the actual code at
`aab94b5`, and the load-bearing ones were reproduced with live PoCs (Path
traversal, runtime auth-disable, credential cross-wiring, cookie attributes,
CSRF reachability, middleware ordering). Items reported in Phase 1 as suspected
were either confirmed or refuted with evidence — see "Refuted / verified-clean"
at the end.

**Headline:** the backend's crypto, audit chain, SQL, and redaction layers are
sound. The serious problems are (a) two file-write sinks that trust a **remote
server's** UIDs, (b) an authentication surface that **cannot actually be enabled**
by any supported operator path, and (c) a **shipped installer whose code is not
the code in this repository** — it lacks the one control the entire
"unauthenticated admin panel" security model rests on.

---

## Findings by severity

| Severity | Count |
|---|---|
| **Critical** | 3 |
| **High** | 6 |
| **Medium** | 7 |
| **Low** | 9 |
| **Total** | **25** |

Top CVSS (base, CVSS v3.1):

| # | Finding | CVSS | Vector |
|---|---|---|---|
| C-1 | Path traversal via remote DICOMweb UIDs (`reports/dicomweb.py:177`) | **9.8** | AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H |
| C-2 | Path traversal via remote PACS C-FIND UIDs (`reports/move.py:170`) | **9.8** | AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H |
| C-3 | Shipped installer lacks the boot-time bind-refusal control | **8.1** | AV:N/AC:H/PR:N/UI:N/S:U/C:H/I:H/A:H |
| H-1 | Auth disableable at runtime, no re-check (`web/routes.py:790`) | **8.7** | AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:H |
| H-2 | Authentication cannot be enabled by any supported path | 7.5 | — |
| H-3 | Redaction sentinel cross-wires / destroys credentials | 6.8 | — |
| H-4 | `bcrypt` undeclared → default single-round SHA-256 admin password | 7.5 | — |
| H-6 | Config secrets written cleartext when no master password is set | 7.4 | AV:L/AC:L/PR:H/UI:N/S:U/C:H/I:N/A:N |
| M-1 | SSRF via server-controlled `Link: rel="next"` pagination | 7.5 | AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:N/A:N |

---

# CRITICAL

## C-1 — Arbitrary file write: remote DICOMweb server controls the output path
**`src/mercure_gateway/reports/dicomweb.py:177-184`** · CWE-73 (External Control
of File Name or Path) · **CVSS 9.8 (AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H)**

`_save` composes the on-disk output path from UIDs parsed verbatim out of a
**remote server's** QIDO-RS JSON response, with no validation:

```python
def _save(self, ds: Any, match: ReportMatch) -> Path:
    sub = "sr" if match.sop_class_uid == SR_SOP_CLASS else "pdf"
    out_dir = self._reports_dir / match.study_uid / sub     # ← attacker-controlled
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{match.sop_instance_uid}.dcm"        # ← attacker-controlled
    ds.save_as(str(path), enforce_file_format=True)
    return path
```

The receive path solved exactly this problem with `validate_uid`
(`spool/__init__.py:62-70`, regex `^[0-9]+(\.[0-9]+)*$`, ≤64 chars); the reports
tree did not. Both `study_uid` **and** `sop_instance_uid` are unvalidated, and
`_json_ui()` (`dicomweb.py:200-206`) copies the server's value verbatim.

### PoC — executed live against the real transport

A malicious/compromised DICOMweb server returns this QIDO-RS row:

```json
{"00080018": {"vr":"UI","Value":["../../../../../../tmp/poc_owned"]}, ...}
```

Result (verbatim from the run):

```
QIDO matches -> [ReportMatch(..., sop_instance_uid='../../../../../../tmp/poc_owned')]
WADO saved   -> ['/tmp/tmp3x6_3zh7/reports/1.2.3/sr/../../../../../../tmp/poc_owned.dcm']
>>> ESCAPED reports dir? True at /tmp/poc_owned.dcm
>>> reports dir listing: [PosixPath('/tmp/tmp3x6_3zh7/reports/1.2.3/sr')]   # nothing inside
```

The file landed **outside** the reports directory. The write target is fully
attacker-chosen; with absolute-ish traversal the attacker reaches any path the
service user can write.

### Impact on a PHI appliance

- **Audit-chain destruction** — `~/.local/share/mercure-gateway/audit-heads.txt`
  (`main.py:230`) is the out-of-DB tamper-evidence anchor. Overwriting it
  invalidates the chain; appending crafted lines forges an "intact" history.
  This is precisely the control that exists to detect PHI-record tampering.
- **PHI store corruption** — the spool SQLite DB and `.dcm`/`.tags` trees can be
  overwritten, breaking store-before-acknowledge accounting.
- **Config overwrite** — `mercure-gateway.json` can be clobbered (integrity/DoS;
  see H-2/H-6 for why a *legible* malicious config is a bigger problem).
- Content is constrained to DICOM-parseable bytes (`dcmread(force=True)` +
  `save_as(enforce_file_format=True)`), so this is an **integrity/availability**
  arbitrary write rather than arbitrary-code content. On Windows, startup files
  are only partially weaponizable for the same reason.

### Reachability — important, and reported honestly

At `aab94b5` the sinks are **latent, not yet live**: `main.py:525` constructs
`ReportRetriever(config.reports, database, spool, audit=audit)` and never injects
`finder`/`mover`, so `_do_retrieve` (`reports/__init__.py:230`) raises
`"report transports (finder/mover) not configured"` and the report goes FAILED
before any remote bytes are fetched. `transport_for_query_source()` and
`build_dicomweb_transport()` exist and are pre-registered
(`reports/transport.py:147-172`) — the wiring is one integration line away, and
`reports.enabled` + `query_source.type: "dicomweb"` is documented, supported
configuration. The severity reflects the intended/supported configuration, not
the current integration gap.

### Remediation

Validate at the trust boundary (where untrusted remote data becomes a
filesystem path), and additionally resolve-and-confine:

```python
# reports/dicomweb.py
from mercure_gateway.spool import InvalidUIDError, validate_uid

class DICOMwebError(Exception): ...

def _validated(uid: str | None, what: str) -> str:
    try:
        return validate_uid(uid or "", what=what)
    except InvalidUIDError as exc:
        raise DICOMwebError(f"remote server returned {exc}") from exc

def _save(self, ds: Any, match: ReportMatch) -> Path:
    study_uid = _validated(match.study_uid, "StudyInstanceUID")
    sop_uid  = _validated(match.sop_instance_uid, "SOPInstanceUID")
    sub = "sr" if match.sop_class_uid == SR_SOP_CLASS else "pdf"
    root = self._reports_dir.resolve()
    out_dir = (root / study_uid / sub)
    path = out_dir / f"{sop_uid}.dcm"
    # Defense in depth: refuse anything that resolves outside the root.
    if path.resolve() != path or not str(path.resolve()).startswith(str(root) + os.sep):
        raise DICOMwebError(f"refusing to write outside the reports directory: {path}")
    out_dir.mkdir(parents=True, exist_ok=True)
    ds.save_as(str(path), enforce_file_format=True)
    return path
```

`reports/move.py:_save` needs the identical treatment. Add regression tests that
feed traversal UIDs through `find()`/`retrieve()` and assert nothing escapes.

---

## C-2 — Arbitrary file write: remote PACS C-FIND response controls the output path
**`src/mercure_gateway/reports/move.py:170-177`** · CWE-73 · **CVSS 9.8
(AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H)**

Same defect, different transport. `_save` builds
`reports/{study_uid}/{sr|pdf}/{sop_uid}.dcm` from values taken verbatim from a
PACS C-FIND response (`reports/find.py:129-137` builds `ReportMatch` from
`getattr(dataset, "StudyInstanceUID")` etc. with no validation). Worse than C-1:
here **both** path components are remote-controlled — the C-FIND `StudyInstanceUID`
need not even be the one the gateway asked for.

### PoC — executed live

```python
r = ReportRetrieve(host="127.0.0.1", port=104, aet="X", store_scp_port=0,
                   store_scp_ae_title="GATEWAY", reports_dir=out)
p = r._save(ds, study_uid="../../../../../../tmp/poc_study",
            sop_class=SR_SOP_CLASS, sop_uid="../../../../../../tmp/poc_sop")
# move._save wrote: /tmp/.../reports/../../../../../../tmp/poc_study/sr/../../../../../../tmp/poc_sop.dcm
# >>> file exists outside reports: True   (resolves to /tmp/poc_sop.dcm)
```

Impact and remediation are identical to C-1. Apply `validate_uid` to
`study_uid`, `series_uid` (used for the C-STORE receive keying) and
`sop_instance_uid` in both `find.py` and `move.py`, and fail the retrieval with
`ReportRetrieveError` on a bad UID rather than writing the file.

---

## C-3 — The shipped installer is not the audited source, and lacks the
bind-refusal control
**`src-tauri/binaries/mercure-gateway/_internal/` (whole tree)** · CWE-1246 /
CWE-1188 (incomplete / inconsistent implementation of a security control) ·
**CVSS 8.1**

`tauri.conf.json` bundles `binaries/mercure-gateway/` into every installer
(`bundle.resources`). That directory is a **PyInstaller snapshot of
v1.1.0-rc1**, not the rc3 source under audit:

```
bundle  __init__.py:  __version__ = "1.1.0-rc1"
source  __init__.py:  __version__ = "1.1.0-rc3"
```

22 of the 35 package modules differ. The security-critical one is `main.py`.
The source's `_enforce_bind_security` (`main.py:99-130`) — the control that
`web/auth.py:91-96` explicitly says its own no-op-when-auth-off behaviour
"rests on" — **does not exist in the shipped binary**. It was still
`_warn_insecure`, which logs and binds anyway:

```python
# SHIPPED rc1 main.py (src-tauri/binaries/.../main.py)
def _warn_insecure(config: GatewayConfig) -> None:
    """Warn about insecure web-panel settings at startup."""
    ui = config.web_ui
    if not ui.auth_enabled and ui.host not in ("127.0.0.1", "localhost"):
        logger.warning("web_ui.auth_enabled is false while binding to %s — ...")
        # ← no SystemExit. The panel binds to the network unauthenticated.
```

So an operator deploying the shipped installer with `web_ui.host: "0.0.0.0"`
gets an **unauthenticated PHI + credentials + start/stop API on the network**,
with no boot refusal — exactly what the rc3 source prevents, and exactly the
precondition that makes H-1, H-2, M-4, M-5 exploitable in the field.

This also invalidates source-level review in general: any fix verified in this
audit (22 modules' worth, including `web/routes.py`, `web/__init__.py`,
`update.py`, `spool/__init__.py`, `reports/*`) is **not what ships**.

### Remediation

1. Delete the committed binary tree from VCS; build it in CI from the tagged
   release commit and attach it to the release artifact instead of carrying it
   in the repo.
2. Add a release gate that asserts the bundled `__version__` equals the release
   tag and that `python -c "import mercure_gateway.main"` in the bundle contains
   `_enforce_bind_security`.
3. Record a SBOM + build provenance (SLSA level 3 for a medical device): pinned
   inputs, PyInstaller spec hash, and the commit SHA.

---

# HIGH

## H-1 — Authentication can be disabled at runtime with no re-check
**`src/mercure_gateway/web/routes.py:769-801`** + **`web/auth.py:88-105`** ·
CWE-638 (Not Using Complete Mediation) · **CVSS 8.7
(AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:H)**

`_enforce_bind_security` runs **once at boot** (`main.py:456`), but
`PUT /api/config` swaps the live config object that `require_auth` reads on
**every request**:

```python
# web/routes.py:790
request.app.state.config = updated          # ← live swap
# web/auth.py:94-96
config: GatewayConfig = request.app.state.config
if not config.web_ui.auth_enabled:
    return                                  # ← no re-check against the startup policy
```

### PoC — executed live

```
unauth GET /api/studies -> 401
login good pw        -> 200 | cookie: True
PUT auth_enabled=False -> 200
>>> UNAUTH GET /api/studies      -> 200
>>> UNAUTH GET /api/config       -> 200
>>> UNAUTH POST /api/system/stop -> 200     ← process control, unauthenticated
>>> persisted on disk: {'auth_enabled': False, 'auth_password_hash': 'sha256$salt$…'}
```

One authenticated request flips a network-bound appliance to unauthenticated
**for the process lifetime** and persists it to `mercure-gateway.json` — so it
survives restart. `POST /api/system/stop` also became reachable, i.e. an
availability attack on a clinical gateway. There is no audit event for the
auth-mode change either (`update_config` emits nothing).

### Remediation

Capture the startup security policy and re-check it on every mutating save:

```python
# web/__init__.py — in create_app(), after the config snapshot
app.state.startup_auth_enabled = config.web_ui.auth_enabled
app.state.startup_bind_host = config.web_ui.host

# web/routes.py — inside update_config()/import_config(), before saving
def _reject_security_regression(request: Request, updated: GatewayConfig) -> None:
    """A runtime config save must not relax the boot-time access policy."""
    startup_auth = request.app.state.startup_auth_enabled
    host = updated.web_ui.host
    loopback = host in ("127.0.0.1", "localhost", "::1")
    if startup_auth and not updated.web_ui.auth_enabled:
        raise HTTPException(403, "cannot disable web_ui.auth_enabled at runtime; "
                                 "restart with the new configuration instead")
    if not updated.web_ui.auth_enabled and not loopback \
            and os.environ.get("MERCURE_GATEWAY_ALLOW_INSECURE_BIND") != "1":
        raise HTTPException(403, "refusing to bind the admin panel to "
                                 f"{host!r} with authentication disabled")
```

Also emit an audit event (`AUTH_CONFIG_CHANGED`) on any `web_ui` mutation.

---

## H-2 — Authentication cannot be enabled by any supported operator path
**`src/mercure_gateway/web/wizard.py:20`**, **`main.py:127-129`**,
**`docs/guides/admin-guide.md:58`** · CWE-754 (Improper Check for Unusual
Conditions) / "documented control not implemented"

The setup wizard has no authentication step — verified in both the backend state
machine and the SPA:

```python
# web/wizard.py:20
_STEPS = ["receiver", "destinations", "reports", "summary"]
# web/src/pages/SetupWizard.tsx:5
const STEPS = ["receiver", "destinations", "reports", "summary"];
```

Yet two operator-facing surfaces point at a step that does not exist:

- `main.py:127-128` — *"Enable web_ui auth (wizard → Setup) or set
  web_ui.host to 127.0.0.1."* (the text an operator sees when the gateway
  refuses to boot)
- `docs/guides/admin-guide.md:58` — *"Enable `web_ui.auth_enabled` and set a
  password hash via the **Setup** wizard."*

There is no CLI alternative (`mercure-gateway set-password` does not exist), and
**no code anywhere in `src/`, `scripts/`, or `tests/` creates a password hash** —
I grepped for `hash_password`, `bcrypt.hashpw`, `gensalt`: zero hits. The only
way to enable auth is to compute a hash out-of-band and hand-edit the JSON
(`docs/guides/secrets-and-env-overrides.md` documents the htpasswd-style
workaround). The model label makes this worse — it advertises a hash format the
default install cannot produce or verify:

```python
# config/__init__.py:401-404
auth_password_hash: str = Field(
    default="",
    description="Bcrypt hash of the web UI password. Set via the setup wizard.",
)
```

### Why this is High, not Low

On a PHI-handling medical device the practical operator flow is "turn auth off to
get unblocked, and never turn it back on" — which is the exact state C-3 and H-1
exploit. Separately, `verify_password` **fails closed** on a malformed hash
(`web/auth.py:75-80`: a hash without two `$` separators returns `False`), so a
mistyped out-of-band hash is a permanent **admin lockout** with no recovery path.

### Remediation

1. Add a real auth step to the wizard (`_STEPS` + `SetupWizard._validate_auth`)
   that takes a plaintext password, hashes it server-side, and writes
   `web_ui.auth_enabled=true` + the hash.
2. Add a `POST /api/auth/password` endpoint and a `mercure-gateway set-password`
   CLI command that both call one shared hashing helper.
3. Fix the three misleading texts (`config/__init__.py:403`, `main.py:127`,
   `admin-guide.md:58`).
4. Return a distinct startup error when `auth_enabled=true` but
   `auth_password_hash` is empty/undecodable, instead of a runtime 401 that
   looks like a wrong password.

---

## H-3 — Redaction-sentinel restore can cross-wire or destroy credentials
**`src/mercure_gateway/web/routes.py:705-766`** · CWE-403 (Exposure of File
Descriptor or Handle) / CWE-672 · **CVSS 6.8**

`_restore_redacted_secrets` matches destinations by name, falling back to **list
position** when the name misses (the documented rename case). If the operator
renames *and* reorders, positional matching writes one destination's secret into
another:

### PoC — executed live

Current config: `lab-sftp`→`SECRET_A`, `pacs-sftp`→`SECRET_B`. Operator renames
both and reorders them in the SPA, leaving the sentinel on both fields:

```
PUT (rename + reorder, sentinel untouched) -> 200
  after save: pacs-sftp-2  host=h2 password=SECRET_A      ← the LAB server's password
  after save: lab-sftp-2   host=h1 password=SECRET_B      ← the PACS's password
```

The gateway will now authenticate to the **PACS** with the **lab server's**
password. That is a silent credential leak to an unrelated third-party system
and an audit trail that attributes the wrong credential to the delivery.

The second failure mode was also reproduced: a sentinel with **no stored
counterpart** is persisted literally, destroying the credential:

```
>>> unmatched sentinel persisted literally: ['***']     # destination "brand-new"
```

An operator who re-imports a redacted export that adds a destination, or who
copies a `***` field, silently bricks that destination — and the failure surfaces
only as an SFTP auth error at forwarding time.

### Remediation

Stop using list position as an identity key. Give every destination a stable,
server-assigned id and match on that; require an explicit
`"unchanged"`-style sentinel that is only valid when a counterpart exists:

```python
for destination in data.get("destinations", []):
    prev = current_dests.get(destination.get("id"))   # stable id, not name or index
    if prev is None and any(destination.get(k) == _REDACTED_SENTINEL
                             for k in DESTINATION_SECRET_FIELDS):
        raise HTTPException(400, f"destination {destination.get('name')!r} is new but "
                                 "carries a redaction sentinel; supply the real credential")
    ...
```

If a positional fallback must stay for one release, restrict it to the case where
*every* destination was renamed with no reordering, and log a warning.

---

## H-4 — `bcrypt` is not a declared dependency; the default admin password is
single-round SHA-256
**`pyproject.toml:7-21`**, **`src/mercure_gateway/web/auth.py:61-80`** ·
CWE-916 (Use of Password Hash With Insufficient Computational Effort) ·
**CVSS 7.5**

`bcrypt` is **absent** from `dependencies` (and from every optional group) in
`pyproject.toml`. It is importable in some environments only as a transitive of
`paramiko`; the PyInstaller bundle happens to freeze it. A plain
`uv pip install .` therefore has **no bcrypt** — and the code silently falls
through to the fallback:

```python
if password_hash.startswith("$2"):
    try:
        import bcrypt
        return bool(bcrypt.checkpw(password.encode(), password_hash.encode()))
    except ImportError:
        return False          # ← bcrypt hashes can NEVER verify; not a fallback
parts = password_hash.split("$", 2)
if len(parts) == 3 and parts[0] == "sha256":
    _algo, salt, expected = parts
    computed = hashlib.sha256((salt + password).encode()).hexdigest()   # ← one round
```

Two distinct problems:

1. **The docstring is wrong.** `web/auth.py:64-66` claims it "falls back to
   salted SHA-256 … so the default install (no bcrypt dependency) still avoids
   storing the plaintext password." For any `$2`-prefixed hash the `except
   ImportError` returns `False` — it **fails closed**, i.e. a permanent login
   lockout, not a downgrade. (Independently confirmed: a hash with only one `$`
   also returns `False` — correct fail-closed behaviour, but undocumented.)
2. **The default path is single-round SHA-256.** One round of SHA-256 over
   `salt+password` is brute-forceable at billions of candidates per second on a
   GPU. Combined with H-6 (the hash sits in cleartext in the config file by
   default) and M-2 (no rate limiting), an attacker who reads
   `mercure-gateway.json` recovers the admin password offline. The config label,
   the admin guide, and `systemd/gateway.env.example` all call it "bcrypt".

### Remediation

```toml
# pyproject.toml
dependencies = [
    "bcrypt>=4.1",
    ...
]
```

```python
# web/auth.py — make the intent match the code
def verify_password(password: str, password_hash: str) -> bool:
    """Verify *password* against a stored hash.

    Requires bcrypt (a declared dependency). bcrypt hashes that cannot be
    verified reject; the salted-SHA-256 legacy format is accepted only for
    upgrades from pre-1.1 configs.
    """
```

And add a one-time upgrade that re-hashes a legacy `sha256$…` value as bcrypt on
the next successful login.

---

## H-5 — Session cookie lacks the `Secure` flag
**`src/mercure_gateway/web/auth.py:121-127`** · CWE-614 (Sensitive Cookie Without
`Secure` Attribute) · **CVSS 6.5**

Verified from a live `Set-Cookie`:

```
mercure_session=1789748330.d5d8…; HttpOnly; Max-Age=43200; Path=/; SameSite=lax
                                                ↑ no Secure
```

`HttpOnly` and `SameSite=Lax` are correct; `Secure` is omitted, presumably for the
loopback plain-HTTP Tauri case. But ADR-0007 explicitly permits a TLS-terminating
reverse proxy in front of the panel, and `docs/guides/admin-guide.md` documents
that deployment. In that topology the cookie is sent in the clear on the **first
hop** (browser → proxy over plain HTTP, or any downgrade), exposing a 12-hour
admin session token. Since the token is an HMAC over only the expiry
(`create_session_token`, `auth.py:39-43`), capturing it is a full admin takeover.

### Remediation

Bind the flag to the transport the panel is actually serving, which the app
already knows:

```python
response.set_cookie(
    _COOKIE_NAME, token,
    max_age=_SESSION_TTL_SEC, httponly=True, samesite="lax",
    secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https",
)
```

Also consider making the token carry an identity claim (not just an expiry) so a
captured token is not replayable across a password change, and shorten the TTL or
add idle timeout.

---

## H-6 — Config secrets are written cleartext to disk by default
**`src/mercure_gateway/config/__init__.py:599-618`**,
**`config/encryption.py:61-78`**, **`web/routes.py:784-788`** · CWE-312
(Cleartext Storage of Sensitive Information) · **CVSS 7.4**

`credentials.encrypted` defaults to `true`, but encryption only fires when
`MERCURE_MASTER_PASSWORD` / `…_FILE` is set in the environment. Nothing in the
wizard, the SPA, or the CLI ever sets it, and `PUT /api/config` calls
`save_config(updated, path)` **without any master-password parameter**
(`routes.py:788`). So the normal operator flow writes SFTP passwords, SSH
**private keys**, passphrases, S3 secret keys, the DICOMweb auth token, the XNAT
password, the hub API key and the admin password hash as **plaintext into
`mercure-gateway.json`** — on a device whose USB-dongle variant is designed to be
physically carried around. The live H-1 PoC confirmed this: the saved file
contained `auth_password_hash` in cleartext.

`GET /api/config` and `/diagnostics/export` do redact these correctly (verified:
`redact_config` covers all six secret field names plus the credential-entry
blocks), so this is an at-rest problem, not an API-exposure problem — which is
exactly what makes it easy to miss.

### Remediation

1. Generate a random master password on first run and store it in the OS keyring
   (the `KeyringCredentialStore` abstraction already exists and fails closed to
   the encrypted vault); fall back to an explicit "I understand" operator
   confirmation for cleartext.
2. Pass a resolvable master-password handle through `update_config` /
   `import_config` rather than re-reading the env at save time.
3. Add a startup lint (`config/lint.py`) that raises a loud warning when
   `credentials.encrypted` is true but a secret field still contains plaintext.

---

# MEDIUM

## M-1 — SSRF / request redirection via server-controlled pagination
**`src/mercure_gateway/reports/dicomweb.py:119, 157-161, 187-197`** · CWE-918 ·
**CVSS 7.5**

The `find()` loop follows whatever URL the remote server puts in the
`Link: rel="next"` header, and `_request` will fetch it verbatim (prepending
`https://` when there is no scheme):

```python
next_url = _parse_link_next(resp)      # ← raw server-controlled URL
...
resp = self._request("GET", url)
if not url.startswith("https://") and not url.startswith("http://"):
    url = f"https://{url}"             # ← a schemeless "//host/path" becomes https://host/path
```

### PoC — executed live

A server replied with `Link: <http://127.0.0.1:9/metadata-internal>; rel="next"`.
The gateway then fetched exactly that URL:

```
find raised: ConnectionError HTTPConnectionPool(host='127.0.0.1', port=9):
   Max retries exceeded with url: /metadata-internal
```

A compromised PACS can therefore pivot the appliance into an internal network
scanner/client — including cloud-metadata endpoints
(`http://169.254.169.254/latest/meta-data/`) — from a DICOMweb response. Because
the response is parsed as DICOM, exfiltration is limited, but reachability
probing and blind request issuance are not.

### Remediation

Constrain pagination to the configured origin:

```python
def _next_page_url(self, resp: requests.Response) -> str | None:
    url = _parse_link_next(resp)
    if url is None:
        return None
    parts = urlsplit(url)
    base = urlsplit(self._base_url)
    if parts.scheme not in ("http", "https") or parts.netloc != base.netloc:
        raise DICOMwebError(f"refusing to follow pagination outside {base.netloc}: {url!r}")
    return url
```

Also set a short connect/read timeout and pin TLS to the configured CA when
`verify_tls` is on.

---

## M-2 — No rate limiting or lockout on `POST /api/login`
**`src/mercure_gateway/web/routes.py:99-110`**, **`web/auth.py:108-119`** ·
CWE-307 · **CVSS 5.3**

Verified live: 30 consecutive wrong passwords all returned 401 with no throttle,
no backoff, no lockout, and no audit event. With H-4 (single-round SHA-256) and
H-6 (cleartext hash on disk), online brute force is only bounded by network
latency. `login()` also accepts **any** password when `auth_enabled=false`
(documented, but it means an auth-disabled box has no tamper signal at all).

### Remediation

Add a per-username/per-IP exponential backoff or a fixed window limiter
(`slowapi` or a small in-memory counter), return 429 after N failures, and emit a
`LOGIN_FAILED` audit event — the audit chain is the right place to surface this
on a device with no SIEM.

---

## M-3 — SFTP handler lets the operator's personal SSH key/agent authenticate
**`src/mercure_gateway/forwarder/handlers/sftp.py:100-115`** · CWE-285 (Improper
Authorization / credential-scope overreach) · **CVSS 6.5**

`client.connect()` is called with **no `timeout`, no `look_for_keys=False`, and
no `allow_agent=False`**. I verified paramiko 5.0.0's auth order in
`SSHClient._auth`:

```
0: configured pkey        → auth_publickey
47: key_filenames         → per-key file
65: if not two_factor and allow_agent:   Agent().get_keys()   ← tried BEFORE password
104: if not look_for_keys: …~/.ssh/id_* (RSA/ECDSA/Ed25519)   ← tried BEFORE password
124: if password is not None: auth_password
```

Because both flags default to `True`, an SSH agent key or `~/.ssh/id_*` on the
appliance is attempted **before** the destination's configured password. If it is
accepted by the remote server, the gateway delivers PHI authenticated as the
**operator's personal key** rather than the configured service account — silently,
with a successful delivery audit event. On a shared appliance any user's key can
become a forwarding credential. The missing `timeout` is an availability problem
too: a stalled SFTP server hangs a forwarder worker indefinitely, and
`forwarding.concurrency` defaults to 3, so three stalled destinations **halt all
PHI delivery**.

(Host-key handling here is correct: `RejectPolicy` with a configured
`known_hosts`, failing closed when no file is configured — verified.)

### Remediation

```python
client.connect(
    hostname=self.destination.host,
    port=self.destination.port,
    username=self.destination.username,
    pkey=key or None,
    password=password if not private_key else None,
    timeout=20,
    auth_timeout=20,
    banner_timeout=20,
    look_for_keys=False,      # never use ambient host credentials
    allow_agent=False,
)
```

---

## M-4 — `auth_password_hash` is settable as an arbitrary pre-computed string
**`src/mercure_gateway/web/routes.py:769-801`**, **`config/__init__.py:401-404`**
· CWE-521 (Weak Password Requirements) / CWE-346 · **CVSS 6.5**

`update_config` validates the payload into `GatewayConfig` and saves it. Nothing
hashes, nothing checks format, nothing requires the value to be a hash at all.
Combined with H-2 (no way to legitimately set a password) this means the only
working flow is: paste a string into `auth_password_hash`. An attacker who gets
one authenticated request — or anyone on an auth-disabled network-bound box — can
install a **known** password (or `""`, which with `auth_enabled=true` makes login
impossible → permanent admin lockout / DoS). Any value that is not `$2…` or
`sha256$salt$hex` makes `verify_password` return `False` for everything.

### Remediation

Add a server-side password-setting endpoint that accepts a **plaintext** password
and stores only the hash (`bcrypt`), and reject `PUT /api/config` bodies that
touch `web_ui.auth_password_hash` — it should never be a client-supplied value.
This also closes the format-mismatch lockout.

---

## M-5 — `/api/echo` is an unrestricted outbound network probe
**`src/mercure_gateway/web/routes.py:1102-1124`**, **`web/echo.py:26-110`** ·
CWE-918 · **CVSS 6.5 (conditional)**

An authenticated operator can make the appliance open a TCP connection and a
DICOM association to **any** `host:port` (`EchoTarget` has no allow-list; the
handler builds a `DICOMDestination` straight from the request body). Response
classification — `ok` / `refused` / `timeout` / `error` — is a network port
scanner with a oracle for distinguishable services, reachable from the browser.
This is legitimate as a wizard feature, but: on an auth-disabled network-bound
appliance (the C-3/H-1 state) it becomes an **unauthenticated** internal-network
scanner mounted on a PHI node, and nothing blocks link-local ranges
(`169.254.169.254`, `127.0.0.1`, RFC1918).

### Remediation

Keep the feature but restrict it to the configured destinations' hosts (the SPA
already sends a destination name), or at minimum reject link-local/loopback
ranges and require the target to be present in `config.destinations`. Rate-limit
per session.

---

## M-6 — CSRF origin allow-list accepts any port on loopback
**`src/mercure_gateway/web/__init__.py:43-51, 110-122`** · CWE-358 ·
**CVSS 5.4**

`_ALLOWED_ORIGIN_HOSTS = {"127.0.0.1", "localhost"}` accepts `http://127.0.0.1:<any
port>`. Verified live: a POST with `Origin: http://127.0.0.1:9999` passed the
origin check (it failed later only on body parsing, with 422 — not on CSRF).
So any other local process, or any local web page served from any loopback port,
can issue state-changing requests against the gateway. On a single-user appliance
this is a deliberate trade-off (documented at `web/__init__.py:40-42`), but it
means the CSRF layer is only a defense against *remote* origins, and on a
multi-user or terminal-services host it is bypassable by other users' processes.

### Remediation

Narrow the fallback to the port the panel is actually bound to (plus an explicit
test override), and treat the SPA origin as the sole allowed origin in
production.

---

## M-7 — Unauthenticated OpenAPI schema exposure
**`src/mercure_gateway/web/__init__.py:161-165`** · CWE-200 · **CVSS 5.3**

Verified live with `auth_enabled=true`: `GET /docs` → 200, `GET /redoc` → 200,
`GET /openapi.json` → 200 (30,329 bytes enumerating every PHI endpoint, every
request/response model, and every config field name) while `GET /api/system/health`
correctly returned 401. FastAPI's doc routes are app-level, so `require_auth`
(declared per-router) never covers them. On a network-bound panel this is a
reconnaissance gift; on loopback it is a minor information disclosure.

### Remediation

```python
app = FastAPI(
    title="QuantumRAD Gateway API", version=__version__, ...,
    docs_url=None if config.web_ui.auth_enabled else "/docs",
    redoc_url=None if config.web_ui.auth_enabled else "/redoc",
    openapi_url=None if config.web_ui.auth_enabled else "/openapi.json",
)
```

---

# LOW

| # | Finding | Location | Detail |
|---|---|---|---|
| L-1 | Absolute server-side `file_path` echoed in PHI responses | `web/routes.py:982, 987, 1003, 1011` | The full host filesystem path of every retrieved report is returned to the browser (also stored in the `reports` table and surfaced in `/reports`). Unnecessary: the client only needs the report id. Strip it from the response model. |
| L-2 | Sessions are never rotated or revocable server-side | `web/auth.py:39-58, 130-132` | `logout()` only deletes the client cookie; the HMAC token stays valid until its 12h expiry. A password change does not invalidate existing sessions (the signing secret is derived from the hash, so *changing* the hash does invalidate them — but there is no explicit revocation list). |
| L-3 | Dev/build-tooling advisories (no production impact) | `web/package.json` | vitest 2.1.9 — **GHSA-5xrq-8626-4rwp, Critical 9.8** (arbitrary file read + code execution when the Vitest UI server is listening; dev-machine only) and GHSA-82fw-gwwq-j7x9 (Med, `@vitest/mocker` path traversal); vite 5.4.21 — GHSA-fx2h-pf6j-xcff (Med-High, `server.fs.deny` bypass), GHSA-v6wh-96g9-6wx3 (Med, `launch-editor` NTLMv2 disclosure on Windows), GHSA-4w7w-66w2-5vf9 (Low); esbuild 0.21.5 — GHSA-67mh-4wv8-2f99 (Med 4.8, dev-server CORS). All are `devDependencies`. Bump vite → `^6.4.3`, vitest → `^4.1.11`, esbuild → `^0.25.0`. |
| L-4 | Test-only 1.3 MB `axe.js` shipped in the production static dir | `web/public/axe.js`, `src/mercure_gateway/web/static/axe.js` | axe-core 4.13.0 (verified byte-identical to the npm package — no tampering), but `axe-core` is a **devDependency** whose built artifact is vendored into the directory `create_app` mounts at `/` (`StaticFiles(html=True)`), so it is served to every browser at `/axe.js` while `npm audit` would report any future advisory as dev-only. Currently untracked (WIP); must not ship. |
| L-5 | Rust transitive advisories, all low, all upstream Tauri | `src-tauri/Cargo.lock` | `glib 0.18.5` soundness (RUSTSEC-2024-0429, Linux GTK webview only); `proc-macro-error`, `unic-char-property`, `unic-char-range`, `unic-common`, `unic-ucd-ident`, `unic-ucd-version` unmaintained (no fix available). Not actionable without an upstream Tauri/wry bump. |
| L-6 | CSP omits `frame-src`, breaking PDF report rendering | `web/__init__.py:68-77`, `web/src/pages/ReportsView.tsx:109-113` | `frame-src` inherits `default-src 'self'`, so the frontend's `data:application/pdf;base64,…` iframe is not `'self'` and should be refused — PDF reports likely render blank. Functional defect, not an XSS hole (React escapes the SR text branch; the iframe is gated on `mime === "application/pdf"`). Add `frame-src 'self' data:` and `sandbox=""` on the iframe. |
| L-7 | Full study UIDs in `title` tooltip attributes | `web/src/pages/ReportsView.tsx:84`, `QueueView.tsx:23` | The visible cell is truncated but the full StudyInstanceUID is in the DOM `title`, trivially screen-grabbed. PHI identifiers — drop the tooltip or show a truncated UID. |
| L-8 | Inverted middleware-ordering comment | `web/__init__.py:179-181` | The comment says the security middleware is "FIRST (runs outermost) … CSRF origin check before the CORS handling". That is factually backwards: `add_middleware` inserts at index 0, so the **last**-added middleware is outermost — `CORSMiddleware` is outermost (verified: `user_middleware` order is `['CORSMiddleware', ...]`). **The Phase 1 concern that the CSRF check might be unreachable is REFUTED**: Starlette's `CORSMiddleware.__call__` passes non-allowed origins straight through to the inner app for non-preflighted requests (`simple_response` just calls `self.app(...)`), so `_SecurityMiddleware` still returns 403. Verified live: `POST /api/login` with `Origin: http://evil.example` → `403 {"detail":"origin not allowed"}`, including for form-encoded (simple) requests. Preflighted `OPTIONS` requests are answered by CORS directly, which is correct since they are not state-changing. Fix the comment; the control works. |
| L-9 | Documentation and model labels claim bcrypt | `config/__init__.py:403`, `docs/guides/admin-guide.md`, `systemd/gateway.env.example`, `web/destinations` UI field label | See H-4/H-2 — the labels describe a capability the default install does not have. |

---

# Verified clean / Phase 1 items refuted

These were checked and are **not** exploitable. Recorded so they are not
re-litigated:

1. **SQL injection** — every query in `spool/db.py` is parameterized; the web
   layer never interpolates into SQL (confirmed in `routes.py` docstring and by
   reading all `Database` methods).
2. **C-STORE path traversal** — closed. `store_instance`
   (`spool/__init__.py:321-323`) validates all three UIDs through `validate_uid`
   before touching the filesystem.
3. **Audit chain** — append-only via `BEFORE UPDATE`/`BEFORE DELETE` triggers
   (`spool/db.py:51-65`); `verify()` recomputes each link from the *previous
   computed* hash, so a row-plus-hash rewrite cannot forge it
   (`audit/__init__.py:278`); `phi_scope` is applied at read time so the chain
   stays verifiable; the head anchor lives outside the spool dir.
4. **Signature verification** — `Updater.verify_signature` is fail-closed in all
   three required cases (empty signature, no trust anchor, mismatch) and
   `_is_newer` closes the downgrade path. `tauri.conf.json`'s
   `"REPLACE_VIA_RELEASE_CONFIG"` placeholder is rejected by `_load_public_key`
   (wrong length → `ValueError`), and `_check_for_updates` wraps the constructor,
   so an unconfigured updater logs a warning and applies nothing.
5. **Signed anchors** — the gateway holds no signing key; `verify_anchor_signatures`
   re-verifies offline against the hub's public key.
6. **XSS in the SPA** — no `dangerouslySetInnerHTML`, `innerHTML`, `eval`, or
   `new Function` anywhere in `web/src`; every server-supplied string (report
   text, audit detail, patient names) renders as escaped JSX children.
7. **Secrets in the client** — no `localStorage`/`sessionStorage` use; no Bearer
   token is ever constructed client-side; `credentials: "include"` is set on every
   request; no analytics/telemetry/Sentry, so no PHI leaves the browser on a crash.
8. **Hardcoded secrets in the repo** — none. All credential-looking literals are
   test fixtures (`REAL-PW`, `s3cret`, `test-key`); `mercure-gateway.json` and its
   three `.bak` files contain empty secret fields; `web/.env.example` is fully
   commented out.
9. **Keyring fallback** — `KeyringCredentialStore` fails closed to the encrypted
   config vault; a missing keyring never degrades to cleartext.
10. **Redaction** — `redact_config` covers all six destination secret fields, the
    credential-entry encrypted blocks, the hub API key, the web-UI hash and both
    public keys; the `***` sentinel round-trips correctly in the non-reordered
    case.
11. **`store_instance` fsync ordering** — the durability barrier (bytes + dir
    entries fsynced before the DB row says RECEIVED) is sound.
12. **Frontend CSRF contribution** — the SPA never submits a plain HTML form and
    every request goes through `apiUrl()` against fixed `/api/...` paths.

---

# Remediation priority

| Priority | Items | Why first |
|---|---|---|
| **P0 — before GA** | C-1, C-2, C-3, H-1, H-2 | Arbitrary file write from a remote server; the shipped artifact lacks the access-control gate the model depends on; auth can be disabled at runtime and cannot be enabled by any supported path. |
| **P1 — GA blocker for networked deployments** | H-3, H-4, H-5, H-6, M-1, M-4 | Credential cross-wiring/loss, brute-forceable admin secret, cleartext secrets at rest, SSRF pivot. |
| **P2 — hardening** | M-2, M-3, M-5, M-6, M-7 | Rate limiting, ambient-SSH-credential suppression, probe scope, CSRF narrowing, schema exposure. |
| **P3 — hygiene** | L-1 … L-9 | Info disclosure, devDep bumps, axe.js removal from the shipped static dir, comment/label corrections. |

**One process note:** C-3 means this audit — and any review performed against
`src/` — describes code the installer does not run. Fixing the findings here
without rebuilding the release artifact from the tagged commit fixes nothing for
already-shipped boxes.
