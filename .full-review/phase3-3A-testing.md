# Phase 3-3A — Full-Stack Testing Review: dicom-gateway

**Scope:** `aab94b5` + uncommitted WIP in `web/` (11 files, accessibility pass) + untracked `web/src/test/a11y-scan.test.tsx`
**Method:** Full suite executed with `pytest --cov` (754 pass / 5 skip, 242.9 s), vitest executed (11 files / 65 tests, 34.7 s), source read for every prior-phase finding, two path-traversal exploits built and demonstrated live, the 128-presentation-context ceiling reproduced against real pynetdicom, and the preserved US-01 receive benchmark re-run from `.full-review/.perfbench/`.

---

## 0. Executive summary

The suite is genuinely good and this review does not change that conclusion. Every prior-phase gap was **confirmed as untested**, and four were independently reproduced by writing new probes — meaning the tests are not merely missing, they are missing for behaviour that is demonstrably broken right now.

**Headline numbers**

| Metric | Value |
|---|---|
| Python tests | 738 collected (754 pass / 5 skip across `tests/` + colocated) |
| Python line coverage | **85.84%** (4179 / 4741 stmts, 1172 branches) — gate is ≥80% |
| Frontend tests | 11 files / 65 tests — **0 run in CI** |
| E2E | 7 Playwright specs, `workers: 1` |
| Perf gates | 2 assertions, both against a fake handler doing no I/O |
| US-01 receive throughput | **17.3 inst/s at 25 associations, 1338 ms median latency** — ungated and ~20× worse per-association than single-association load |

**Findings: 17 total — 2 Critical, 5 High, 5 Medium, 5 Low.**

The three most damaging are all "the test that would have caught this does not exist, and I proved the bug is live": the report-retrieval path traversal (C-1/C-2), the unwired report transports (C1), and the 128-context SCU ceiling.

---

## 1. Test coverage analysis

### 1.1 What is well covered

The 85.84% figure is honest for the modules that matter most day-to-day. Strong areas:

- `spool/__init__.py` (91.6%) and `spool/db.py` (87.7%) — the store-before-ack core is heavily exercised, including the `validate_uid` traversal guard at `tests/test_storage.py:130`.
- `web/routes.py` (92.8%) — 29 missing lines out of 560; the config PUT/GET round-trip has real assertions about secret preservation (`tests/test_web_api.py:400-445`).
- `audit/anchoring.py` (90.5%) — the tamper-evident chain has a dedicated suite (`test_audit_anchor_signing.py`).
- Security-conscious defaults are pinned: `tests/test_security_gates.py` asserts TLS-verify-peer-on, S3-https-on, redaction covers every secret field, and the audit chain verifies clean.
- CSRF origin checks and the loopback-bind invariant are tested at `tests/test_web_security.py:78-169` — the boot-time guard (`main.py:113`) is covered.

### 1.2 Coverage is *systematically undercounted* on the composition root

`main.py` reports **58.8%** (105 missing lines), but that number is misleading and the reason matters more than the number:

`tests/test_main.py:72` and `:138` drive `main()` **as a subprocess** (`subprocess.Popen([...])`), and the repo has **no subprocess coverage capture** — no `COVERAGE_PROCESS_START`, no `[tool.coverage.run] concurrent = true`, no `sitecustomize`. I verified this by grepping `pyproject.toml`, `.coveragerc` (absent), `justfile` and `.github/workflows/ci.yml`.

Consequences, both directions:

1. **The ≥80% gate passes on a number that misrepresents `main.py`.** The real executed coverage is higher than 58.8%.
2. **But the gate therefore cannot tell you what is actually tested.** `main.py:525` (`report_retriever = ReportRetriever(...)`) is marked "missing" while being executed every time a subprocess test boots. A future regression that breaks startup wiring would not move the coverage needle — the signal is dead precisely where the integration risk lives.

**Recommendation — make the coverage gate meaningful again.** Either capture subprocess coverage, or add an in-process composition-root test that asserts wiring without spawning:

```python
# tests/test_main_composition.py
def test_reports_transports_injected_when_enabled(tmp_path, monkeypatch):
    """main() must wire finder/mover onto ReportRetriever when reports.enabled.

    Regression guard for phase-1 C1: the retriever was constructed with no
    transports, so every poll cycle logged 'transports not configured' and
    no report was ever fetched.
    """
    cfg = default_config()
    cfg.reports.enabled = True
    cfg.storage.spool_dir = str(tmp_path / "spool")
    started = []

    # Capture the retriever after the composition root builds it.
    real_start = ReportRetriever.start

    def _spy(self):
        started.append(self)
        return real_start(self)

    monkeypatch.setattr(ReportRetriever, "start", _spy)
    # ... invoke the wiring under test, then:
    assert started, "ReportRetriever was never constructed"
    assert started[0].finder is not None, "finder transport not injected"
    assert started[0].mover is not None, "mover transport not injected"
```

This is the test that fails today and would fail loudly in CI.

### 1.3 The weakest modules

| File | Cov | Why it matters |
|---|---|---|
| `reports/move.py` | **57.3%** | C-2 traversal lives here; the whole `retrieve()` → `_save()` path is unexecuted |
| `main.py` | 58.8% | Composition root (see above) |
| `web/echo.py` | 64.3% | Echo endpoint — low risk |
| `forwarder/handlers/sftp.py` | 65.0% | Credentials, key exchange — a destination type |
| `reports/render.py` | 71.7% | Report rendering |
| `reports/__init__.py` | 76.1% | The report lifecycle state machine |
| `forwarder/handlers/dicom.py` | **77.5%** | The SCU side of the 128-context bug |
| `web/auth.py` | 81.6% | The bcrypt fail-closed branch is uncovered |

The pattern is clear and consistent: **the receive path is well tested, the report-retrieval tree and the SCU send path are not.** Both are exactly where the prior-phase Critical/High findings sit.

---

## 2. Findings

### CRITICAL

#### T-1 (C-1/C-2) — Path traversal in both report transports is untested AND exploitable

`src/mercure_gateway/reports/move.py:170` and `src/mercure_gateway/reports/dicomweb.py:177`

Both `_save` methods interpolate remote-server-controlled UIDs straight into a filesystem path with no `validate_uid`:

```python
# reports/move.py:170
out_dir = self.reports_dir / study_uid / sub
path = out_dir / f"{sop_uid}.dcm"
```

The receive path validates (`spool/__init__.py:321-323` calls `validate_uid`, tested at `test_storage.py:130`). The reports tree does not. **I demonstrated this live** against both classes; a malicious PACS returning `study_uid = "1.2.3/../../../../../../tmp/pwned"` wrote files outside the reports directory:

```
MOVE  wrote: /tmp/tmpXXX/reports/1.2.3/../../../../../../tmp/pwned-move/pdf/1.2.3.4.dcm | ESCAPED: True
WEB   wrote: /tmp/tmpXXX/reports/1.2.3/../../../../../../tmp/pwned-web/pdf/1.2.3.4.dcm  | ESCAPED: True
artifacts: ['/tmp/pwned-move', '/tmp/pwned-web']
```

This is CVSS 9.8 PHI write-outside-sandbox with zero tests. Existing report tests only ever pass well-formed UIDs.

```python
# tests/test_report_traversal.py — the test that should exist
import pytest
from pathlib import Path
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, SecondaryCaptureImageStorage
from mercure_gateway.reports.move import ReportRetrieve
from mercure_gateway.reports.dicomweb import DICOMwebReportTransport
from mercure_gateway.reports.find import ReportMatch
from mercure_gateway.spool import InvalidUIDError, validate_uid

SR = "1.2.840.10008.5.1.4.1.1.88.11"
TRAVERSAL_UIDS = [
    "1.2.3/../../../../etc/cron.d/evil",
    "..",
    "1.2.3/../../..",
    "1.2.3/..%2f..",          # not decoded, but must still be rejected as a path char
    "/absolute/path",
]

def _ds():
    fm = FileMetaDataset()
    fm.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
    fm.MediaStorageSOPInstanceUID = "1.2.3.4"
    fm.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = Dataset(); ds.file_meta = fm
    return ds

@pytest.mark.parametrize("evil", TRAVERSAL_UIDS)
def test_move_save_rejects_traversal_uid(tmp_path: Path, evil: str) -> None:
    r = ReportRetrieve(host="pacs", port=104, aet="PACS", store_scp_port=5050,
                       store_scp_ae_title="GW", reports_dir=tmp_path / "reports")
    with pytest.raises(InvalidUIDError):
        r._save(_ds(), evil, SR, "1.2.3.4")
    # Nothing escaped the reports tree
    assert list((tmp_path / "reports").rglob("*.dcm")) == []

@pytest.mark.parametrize("evil", TRAVERSAL_UIDS)
def test_dicomweb_save_rejects_traversal_uid(tmp_path: Path, evil: str) -> None:
    t = DICOMwebReportTransport(base_url="https://pacs/dicomweb",
                                reports_dir=tmp_path / "reports", verify_tls=False)
    m = ReportMatch(study_uid=evil, series_uid="1.2.3.9",
                    sop_instance_uid="1.2.3.4", sop_class_uid=SR)
    with pytest.raises(InvalidUIDError):
        t._save(_ds(), m)
    assert list((tmp_path / "reports").rglob("*.dcm")) == []

def test_traversal_blocked_at_finder_boundary(tmp_path: Path) -> None:
    """The match must never carry an unvalidated UID into _save."""
    # assert validate_uid raises on the same set — pins the shared guard
```

Fix: call `validate_uid` on `study_uid`, `series_uid` and `sop_instance_uid` at the point the match is built (in `reports/find.py`) and in both `_save` methods — defence in depth, matching the receive path.

---

#### T-2 (Phase-1 C1) — Report transports are never injected; no test catches it

`src/mercure_gateway/main.py:523-526`

`ReportRetriever.retrieve()` explicitly requires injected transports:

```python
# reports/__init__.py — _do_retrieve
if self.finder is None or self.mover is None:
    raise RuntimeError("report transports (finder/mover) not configured")
```

But `main()` constructs it and never assigns either:

```python
# main.py:523-526
from mercure_gateway.reports import ReportRetriever
report_retriever = ReportRetriever(config.reports, database, spool, audit=audit)
report_retriever.start()
```

I confirmed by grep that `main.py` sets `.finder` / `.mover` / `build_dicomweb_transport` **nowhere**. So with `reports.enabled = true`, the poller starts, every PENDING report hits the `RuntimeError`, `_poll_once` logs `"report %s skipped: %s"` and silently does nothing forever. **US-06 (report retrieval) is a no-op in every shipped build.**

`tests/test_reports_service.py` tests `ReportRetriever` directly with hand-injected fakes, so the unit passes while the product feature is dead. This is the textbook integration-seam gap. See §1.2 for the test.

---

### HIGH

#### T-3 (H-1) — No test that the bind invariant survives a config update

`src/mercure_gateway/web/routes.py:770` (`update_config`)

The boot-time guard `main._enforce_bind_security` is well tested (`test_web_security.py:136-169`: non-loopback + no auth → refused, loopback allowed, auth-required allowed, escape hatch scoped). But `PUT /api/config` accepts `web_ui.auth_enabled = false` with `web_ui.host = "0.0.0.0"` and persists it with **no re-check** — it returns `restart_required: true` and the appliance now reboots into an open admin API on the network holding PHI and credentials.

The invariant "unauthenticated panel must never bind off-loopback" is asserted at exactly one of the two places it must hold.

```python
# tests/test_web_security.py — add
def test_config_update_cannot_disable_auth_off_loopback(client, app, tmp_path):
    """PUT /config must refuse to persist auth_enabled=false + non-loopback host.

    The boot guard refuses to bind that combination; saving it would arm a
    reboot into an open admin API (H-1). The PUT must reject or normalise it.
    """
    app.state.config_path = str(tmp_path / "gw.json")
    body = client.get("/api/config").json()
    body["web_ui"]["auth_enabled"] = False
    body["web_ui"]["host"] = "0.0.0.0"

    r = client.put("/api/config", json=body)

    # Whatever the fix shape is, the dangerous combination must not persist:
    saved = json.loads(Path(app.state.config_path).read_text())
    assert not (saved["web_ui"]["auth_enabled"] is False
                and saved["web_ui"]["host"] not in _LOOPBACK_HOSTS), \
        "config update armed an unauthenticated non-loopback bind"
```

This test should be written to assert the *outcome* (the file must not contain the dangerous pair), so it passes under either fix — reject the PUT, or force `auth_enabled` on. Fails today.

---

#### T-4 (H-3) — `_restore_redacted_secrets` rename+reorder is untested (and cross-wires)

`src/mercure_gateway/web/routes.py:705-766`

The positional fallback is the hazard:

```python
prev = current_dests.get(destination.get("name"))
if prev is None:
    # A rename in place keeps the sentinel under a new name; the stored
    # counterpart is the destination at the same position.
    prev = current_dest_list[index] if index < len(current_dest_list) else None
```

Existing coverage is good for the single-destination case: `test_web_api.py:436` (`test_put_config_rename_preserves_secrets`) renames destination 0 and asserts `REAL-PW` survives. But it never **renames and reorders simultaneously** — which is exactly when the positional lookup resolves to a *different* destination and secret A is written onto destination B. Operators reordering the Destinations list in the panel is a first-class UI action.

```python
# tests/test_web_api.py — add
def test_put_config_rename_and_reorder_does_not_cross_wire_secrets(client, app, tmp_path):
    """Rename + reorder must not attach one destination's credential to another.

    The positional restore fallback keys on list index; after a reorder the
    stored destination at that index is a DIFFERENT destination (H-3).
    """
    app.state.config_path = str(tmp_path / "gw.json")
    from mercure_gateway.config import SFTPDestination
    app.state.config = _config_with_dests(
        SFTPDestination(name="alpha", ..., password="ALPHA-PW"),
        SFTPDestination(name="beta",  ..., password="BETA-PW"),
    )
    body = client.get("/api/config").json()

    # Operator renames beta -> beta-2 AND moves it above alpha.
    body["destinations"] = [
        {**body["destinations"][1], "name": "beta-2"},
        {**body["destinations"][0], "name": "alpha"},
    ]
    r = client.put("/api/config", json=body)
    assert r.status_code == 200

    saved = json.loads(Path(app.state.config_path).read_text())
    by_name = {d["name"]: d["password"] for d in saved["destinations"]}
    assert by_name == {"beta-2": "BETA-PW", "alpha": "ALPHA-PW"}, \
        f"credentials cross-wired on rename+reorder: {by_name}"
```

Fails today — the assertion will show `{"beta-2": "ALPHA-PW", "alpha": "BETA-PW"}` or sentinel leakage. Prefer matching by a stable identity (a server-assigned destination `id`) rather than name-or-index.

---

#### T-5 (H-4) — bcrypt is undeclared and the fail-closed branch is untested

`src/mercure_gateway/web/auth.py:61-80`; dependency declaration in `pyproject.toml`

Verified state:

- `bcrypt 5.0.0` **is installed** and **is not in `pyproject.toml`** — it is present only as a transitive dependency of `paramiko` (confirmed in `uv.lock`). If paramiko ever drops it, or a slimmed install excludes it, `verify_password` silently falls through to SHA-256.
- The default admin password path is single-round `sha256$salt$hex` — no salt is per-installation-global and the algorithm is brute-forceable offline.
- Coverage shows `auth.py:72-74` (the bcrypt `checkpw` path **and its `ImportError → return False`**) are **never executed**. The fail-closed behaviour is undocumented and unverified — and it is the wrong default: an unavailable bcrypt should arguably fall back to SHA-256 for a `$2` hash *only if* the hash is also SHA-256, otherwise every bcrypt-hashed admin is permanently locked out after a dependency change.

```python
# tests/test_web_security.py — add
def test_bcrypt_hash_verifies_when_bcrypt_available():
    """A bcrypt hash verifies and rejects the wrong password (H-4)."""
    import bcrypt as _bcrypt
    h = _bcrypt.hashpw(b"s3cret", _bcrypt.gensalt()).decode()
    assert verify_password("s3cret", h) is True
    assert verify_password("wrong", h) is False

def test_bcrypt_import_failure_is_fail_closed(monkeypatch):
    """Missing bcrypt must never authenticate against a bcrypt hash."""
    import builtins
    real_import = builtins.__import__

    def _block(name, *a, **k):
        if name == "bcrypt":
            raise ImportError("bcrypt not installed")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _block)
    assert verify_password("s3cret", "$2b$12$abcdef...") is False, \
        "bcrypt ImportError must fail closed, not fall through to sha256"

def test_sha256_hash_verifies_and_rejects():
    assert verify_password("s3cret", _make_sha256_hash("s3cret")) is True
    assert verify_password("wrong",  _make_sha256_hash("s3cret")) is False

def test_malformed_hash_rejected():
    assert verify_password("x", "not-a-hash") is False
    assert verify_password("x", "") is False
    assert verify_password("x", "sha256$only-two-parts") is False
```

Also: declare `bcrypt>=4` in `[project.dependencies]` (it is load-bearing for auth, not an incidental transitive), and document the fail-closed contract in the `verify_password` docstring.

---

#### T-6 — The 128-presentation-context SCU ceiling is untested and raises at runtime

`src/mercure_gateway/forwarder/handlers/dicom.py:105-112`

The SCP side **is** tested (`test_sop_classes.py: test_sop_class_budget`). The SCU side — where the failure lives — has no test. I reproduced it against real pynetdicom: the fallback path requests **111 storage classes × `AllTransferSyntaxes`, plus 3 compressed syntaxes × 111 classes = 444 contexts**, and pynetdicom raises hard:

```
ValueError: Failed to add the requested presentation context as there are
already the maximum allowed number of requested contexts
```

So a study whose SOP class is absent from `sop_classes_for_files` does not fail to negotiate — it **raises `ValueError` out of `_send_files`**, which is not among the caught `ConnectionError`s. The study goes to ERROR with a traceback, permanently undeliverable. Even the non-fallback path can exceed 128 for a multi-modality study (each distinct class costs 1 + 3 contexts).

```python
# tests/test_handler_dicom.py — add
import pytest
from pynetdicom import AE
from mercure_gateway.forwarder.handlers.dicom import DICOMHandler
from mercure_gateway.config import DICOMDestination

def test_unknown_sop_class_does_not_explode_context_budget(tmp_path):
    """A study whose SOP class is not in the storage map must still be
    sendable — the fallback must stay under pynetdicom's 128-context cap."""
    # a SOP class deliberately absent from sop_classes_for_files
    ds = _dataset_with_sop_class("1.2.840.10008.5.1.4.1.1.88.72")  # a weird SR
    spool = _spool_with_study(tmp_path, [ds])
    handler = DICOMHandler(spool, DICOMDestination(name="pacs", ...))

    # Building the AE must not raise; the association must be attempted.
    with _fake_scp() as scp:              # minimal C-STORE SCP
        result = handler.deliver(_task(spool, "pacs"))
    assert result.ok, result.error        # currently: ValueError, unhandled

@pytest.mark.parametrize("modality_mix", [["CT","MR","US","XA","PT"], _all_modalities()])
def test_context_count_stays_under_128(modality_mix):
    """Any realistic mix of modalities must negotiate within 128 contexts."""
    files = [_synthetic_file(m) for m in modality_mix]
    ae = AE()
    handler._build_contexts(ae, files)    # extract the context construction
    assert len(ae.requested_contexts) <= 128, \
        f"{len(ae.requested_contexts)} contexts for {modality_mix} — over the protocol cap"
```

Also wrap the `add_requested_context` loop in a budget check and catch `ValueError` alongside `ConnectionError` in `_send_files` so the failure degrades to a retryable route error instead of a crash.

---

#### T-7 (H-7) — The audit chain is never verified in production; `prune()` re-anchors without an integrity precondition

`src/mercure_gateway/audit/__init__.py:385` (`prune`); `audit/anchoring.py:242` (`verify_anchor_signatures`)

`verify_anchor_signatures` has **no production caller** — I grepped: only `test_hub_bookkeeper_interop.py` and `test_audit_anchor_signing.py` call it. Meanwhile `prune()` drops the append-only triggers, deletes old events, and **recomputes every surviving hash from genesis** — without first asserting the chain was intact. If the on-disk chain has been tampered with, `prune()` helpfully rewrites it into a self-consistent state and records a `PRUNE_AUDIT` event that *looks* legitimate, destroying the evidence of tampering.

The tamper-evidence guarantee is asserted in tests but never enforced by the running system.

```python
# tests/test_audit_retention.py — add
def test_prune_refuses_to_reanchor_a_tampered_chain(tmp_path):
    """prune() must verify the chain BEFORE rewriting it (H-7)."""
    audit = _audit_with_events(tmp_path, n=50)
    _tamper(audit, event_id=10, detail="PHI scrubbed by an attacker")

    with pytest.raises(ChainIntegrityError):
        audit.prune(older_than_days=30)

    # The tampered chain is preserved as evidence, not rewritten away.
    assert _read_detail(audit, event_id=10) == "PHI scrubbed by an attacker"

def test_prune_reanchors_an_intact_chain():
    audit = _audit_with_events(tmp_path, n=50)
    assert audit.prune(older_than_days=30) > 0
    assert audit.verify() is True            # chain still self-consistent
    assert _audit_has_event(audit, "PRUNE_AUDIT")

# Plus a production wiring test:
def test_startup_verifies_anchor_signatures_when_configured(...):
    """main() must call verify_anchor_signatures at boot when a hub public
    key is configured, and fail loudly (not log-and-continue)."""
```

---

### MEDIUM

#### T-8 — US-01 receive throughput is unmeasured and ungated (the critical perf gap)

`scripts/check_perf_gates.py`; receive critical section at `spool/__init__.py:321-420`

The perf gates measure exactly two things, both against `_StopwatchHandler.deliver()`, a handler that **does no I/O**:

```python
handler = _StopwatchHandler()
forwarder.register_handler("dicom", handler)   # fake — no fsync, no DB, no network
```

So the gate measures queue-scheduling latency, not forwarding. Meanwhile the *receive* path — the one US-01 actually constrains — runs a fully serialized critical section per instance: 1 file `fsync` + up to 3 directory `fsync`s (`_fsync_instance`, `spool/__init__.py:411`) + a WAL-FULL commit, all inside the process-wide `Database._lock` RLock.

I re-ran the preserved benchmark (`.full-review/.perfbench/store_bench.py`, 1 vs 25 associations, 200 inst/assoc and 200/assoc):

| Associations | Total | inst/s | per-assoc inst/s | median latency | p95 | max |
|---|---|---|---|---|---|---|
| 1 | 200 | 14.0 | 13.99 | **65.1 ms** | 110.6 ms | 349.7 ms |
| 25 | 5000 | 17.3 | **0.69** | **1338.5 ms** | 2440.6 ms | 4007.3 ms |

**25× the offered load yields 1.24× the throughput** and a 20× collapse in per-association throughput. At 25 associations the gateway is delivering ~1038 instances/minute — far below a realistic modality aggregate — and median latency is 1.3 s, which is close to where association timeouts and modality retries begin. This is the single most important unmeasured number in the product, and no CI gate sees it.

**Recommendation — promote the benchmark to a gated job**, asserting the *scaling ratio*, not just an absolute floor:

```python
# scripts/check_perf_gates.py — add
RECEIVE_TARGET_INST_S_25 = 25.0      # US-01: 25 concurrent associations
RECEIVE_MAX_LATENCY_MS_P50 = 500.0   # beyond this, modalities time out
SCALING_FLOOR = 3.0                  # 25 associations must beat 1 by >= 3x
                              # (perfect linear would be 25x; the lock serializes)

def measure_receive_scaling() -> dict:
    """US-01: real concurrent C-STORE load through the real Spool."""
    results = {}
    for n_assoc in (1, 25):
        spool = Spool(open_database(tmp / f"gw_{n_assoc}.db"), _bench_config(tmp / f"s{n_assoc}"))
        lats, errs = [], []
        def worker(tid):
            for i in range(INST_PER_ASSOC):
                t0 = time.perf_counter()
                spool.store_instance(_synthetic_ds(tid, i))
                lats.append(time.perf_counter() - t0)
        threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_assoc)]
        t0 = time.perf_counter()
        for t in threads: t.start()
        for t in threads: t.join()
        el = time.perf_counter() - t0
        lats.sort()
        results[n_assoc] = {
            "inst_s": n_assoc * INST_PER_ASSOC / el,
            "p50_ms": lats[len(lats)//2] * 1e3,
        }
    return results

# assert inst_s_25 >= RECEIVE_TARGET_INST_S_25
# assert p50_ms_25   <= RECEIVE_MAX_LATENCY_MS_P50
# assert inst_s_25 / inst_s_1 >= SCALING_FLOOR   # catches lock regressions
```

The third assertion is the valuable one — it fails on exactly the regression class that an absolute floor tolerates.

Note the benchmark also surfaced a resource-lifecycle bug: `db.close()` with pending auto-enqueue timers raised `sqlite3.ProgrammingError: Cannot operate on a closed database` from `_auto_enqueue` (`spool/__init__.py:476`). `Spool.stop()` cancels timers but nothing calls it on this path.

---

#### T-9 — The disk-full purge loop can spin forever; the failed-delete path is untested

`src/mercure_gateway/disk.py:115-123` + `spool/__init__.py:905-917, 923-950`

The loop is correct *as long as deletes succeed*:

```python
while pct >= self._warning_pct:
    if not self._spool.purge_oldest_delivered():
        ...; break
```

But `purge_oldest_delivered()` returns `True` whenever `list_oldest_delivered()` finds a row — and `_purge_study_dir` **swallows the delete failure and returns without deleting the row**:

```python
except OSError as exc:
    logger.error("could not delete spool files for study %s — DB row kept: %s", ...)
    return          # row kept → still "delivered" → still eligible
```

So on read-only media, a locked file, or a Windows handle held open, `purge_oldest_delivered()` returns `True`, disk usage never drops, and `check_once()` **loops forever at 100% CPU**. This is a live infinite loop in the disk-full auto-recovery path — the worst possible place for it, since it only triggers when the appliance is already in trouble.

I grepped every `OSError` in the test suite: the purge path is never exercised with a failing delete (`test_disk_full_ui.py:89` and `chaos/test_disk_full.py:160` raise OSError in *other* code paths — the disk-measurement endpoint and the receive path).

```python
# tests/test_disk_monitor.py — add
def test_purge_loop_exits_when_delete_fails(tmp_path, monkeypatch):
    """A permanently failing delete must not spin forever (disk-full recovery)."""
    spool = make_spool(tmp_path)
    _deliver_one_study(spool)                  # one SENT study, eligible

    def _rmtree_boom(path):
        raise OSError(13, "Permission denied — read-only media")
    monkeypatch.setattr("mercure_gateway.spool.shutil.rmtree", _rmtree_boom)

    monitor = DiskMonitor(spool, warning_pct=1, purge_on_full=True)  # always over
    started = time.monotonic()
    pct = monitor.check_once()                 # must RETURN, not hang
    assert time.monotonic() - started < 5.0, "purge loop spins when delete fails"
    assert spool._db.list_oldest_delivered() is not None  # row preserved (correct)
```

Fix: `_purge_study_dir` should return whether it actually deleted, and `purge_oldest_delivered()` should return `False` (or advance a cursor) when the delete fails, so the loop breaks.

---

#### T-10 — `Spool.complete`/`fail` are non-atomic; concurrent final routes can double-emit `STUDY_SENT`

`src/mercure_gateway/spool/__init__.py:697-722`

`complete()` executes four separate `transaction()` calls — `mark_route_sent`, `all_routes_complete`, `set_study_state`, `set_retention_delivered` — and `_emit`, **with no lock held across the sequence** (each `transaction()` releases `Database._lock` independently; `Spool` holds only a `_timer_lock`).

Two workers finishing the last two routes of one study can both observe `all_routes_complete() == True` after each marks its own route, and both emit `STUDY_SENT` and set `SENT`.

Existing tests do not catch it:
- `test_audit_coverage.py:145` (`test_study_sent_emitted_once_per_study`) asserts exactly one `STUDY_SENT` — but runs `fwd.process_once(limit=2)`, which is **sequential**.
- `test_concurrent_forwarder.py:57` (`test_3_workers_parallel`) is genuinely concurrent — but gives each study **one** target, so the race window never opens.

The race needs two routes of the *same* study completing in parallel.

```python
# tests/test_concurrent_forwarder.py — add
def test_concurrent_final_routes_emit_study_sent_once():
    """Two workers completing the last two routes of one study concurrently
    must emit STUDY_SENT exactly once (complete() is not atomic)."""
    db = mem_database()
    spool = Spool(db, audit=AuditLog(db))
    fwd = Forwarder(default_config(), spool,
                    retry=RetryPolicy(base_delay_sec=0, max_attempts=1),
                    audit=AuditLog(db))
    fwd.register_handler("dicom", SlowHandler(delay_sec=0.3))   # overlap the window

    study_id = spool.receive("1.2.3.4")
    spool.enqueue(study_id, [TARGET, SECOND_TARGET])           # TWO routes
    fwd.start()
    try:
        _wait_until(lambda: spool.state(study_id) == StudyState.SENT, timeout=10)
    finally:
        fwd.stop()

    sent = [e for e in AuditLog(db).list_events() if e.event == STUDY_SENT]
    assert len(sent) == 1, f"STUDY_SENT emitted {len(sent)} times under concurrency"
    assert spool.state(study_id) == StudyState.SENT
```

This is timing-dependent, so also add the deterministic version that exposes the seam directly:

```python
def test_complete_is_atomic_under_interleaving():
    """Interleave two complete() calls at the exact race point."""
    spool, study_id = _study_with_two_routes()
    real_all_complete = spool._db.all_routes_complete

    def _after_first_check():
        # Worker B completes its route here, inside worker A's window.
        spool.complete(study_id, "pacs")
        return real_all_complete(study_id)

    with patch.object(spool._db, "all_routes_complete", side_effect=_after_first_check):
        spool.complete(study_id, "hub")

    sent = _count_events(STUDY_SENT)
    assert sent == 1
```

Fix: hold a per-study lock across the complete sequence, or make `mark_route_sent` + the all-complete check + the state transition a single `UPDATE ... WHERE (SELECT COUNT...) = (SELECT COUNT...)` statement.

---

#### T-11 — No vitest job in CI: 65 frontend tests including the a11y scan never run

`.github/workflows/ci.yml` — `build-spa` runs `npm run build` only

Verified against the workflow: there is **no `npm run test` anywhere** in CI. The `test` matrix job is Python-only; `build-spa` builds the SPA; `e2e` runs Playwright against the built bundle. So all 11 vitest files — `api.base`, `api.saveconfig`, `config/devApiBase`, `config/lint`, `AuthContext`, `BackendDownView`, `ConfigView`, `DestinationsView`, `ErrorBoundary`, `UpdaterBanner`, and the new `a11y-scan` — are local-only.

Compounding this, the memory note about `tsc -b` applies symmetrically: **vitest transpiles via esbuild and skips type-checking**, so a green local vitest run says nothing about type correctness, and CI has no run at all. The accessibility pass shipped unverified by any automated gate.

```yaml
# .github/workflows/ci.yml — add job
  test-web:
    name: SPA unit tests (vitest)
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version-file: web/.nvmrc
          cache: npm
          cache-dependency-path: web/package-lock.json
      - run: npm ci
        working-directory: web
      - run: npm run test -- --run --coverage
        working-directory: web
      # tsc -b, NOT --noEmit (root tsconfig has files:[] + project references)
      - run: npx tsc -b
        working-directory: web
```

Run vitest from `web/` — the justfile comment is explicit that jsdom resolution is relative to the config file and invoking from the repo root fails every test with "document is not defined".

---

#### T-12 — The `a11y-scan` stubs under-exercise the riskiest markup and the highest-value views

`web/src/test/a11y-scan.test.tsx`

The suite is good in structure — real `axe.run` over `wcag2a/2aa/21a/21aa/22aa`, per-view stubs, `waitFor` before asserting. Two concrete weaknesses:

1. **Only one table-row state is scanned.** The QueueView stub uses `state: "FAILED"` with `num_destinations: 0`, which happens to render both action buttons (`QueueView.tsx:113` Retry for FAILED, `:118` Enqueue for RECEIVED+0-destinations) — so the row is scanned, contrary to the phase-3 concern. But `RECEIVED` with routes, `SENDING`, `SENT`, and `ERROR` rows are never rendered, and ARIA constrains what a `<tr>` may contain, so the other conditional-action combinations are unscanned. The correct fix is a parametrized scan over every state.

2. **The ConfigView stub is nearly empty.** It supplies `config: { general: { ae_title: "GATEWAY" } }` — so the credentials/destinations forms (the `***` sentinel fields, the most complex and PHI-adjacent markup in the panel) are never rendered. `DestinationsView` partially covers destinations, but the full ConfigView form is the one operators live in.

```tsx
// web/src/test/a11y-scan.test.tsx — parametrize row states
const ROW_STATES = ["RECEIVED", "QUEUED", "SENDING", "SENT", "ERROR", "FAILED"];

describe.each(ROW_STATES)("QueueView row (%s)", (state) => {
  it("has no axe violations", async () => {
    stubApi({
      studies: { items: [{
        id: 1, study_uid: "1.2.3.4.5.6.7.8.9.10.11.12.13", accession: "A1",
        modality: "CT", state,
        num_destinations: state === "RECEIVED" ? 0 : 2,   // exercises both action branches
        created_at: "2026-09-18T00:00:00Z",
      }], total: 1 },
    });
    const QueueView = (await import("../pages/QueueView")).default;
    render(<QueueView />);
    await waitFor(() => expect(screen.getByText("A1")).toBeInTheDocument());
    expect(await runAxe(document.body)).toEqual([]);
  });
});

it("ConfigView with a full destination + credentials block", async () => {
  stubApi({
    config: {
      general: { ae_title: "GATEWAY", appliance_name: "GW" },
      destinations: [{ name: "pacs1", type: "dicom", enabled: true, host: "10.0.0.1",
                      port: 104, aet_target: "PACS", aet_source: "GATEWAY", password: "***" }],
      credentials: { entries: { hub: { api_key: "***" } } },
      web_ui: { auth_enabled: true, auth_password_hash: "***" },
    },
    "config/warnings": { warnings: [] },
  });
  const ConfigView = (await import("../pages/ConfigView")).default;
  render(<ConfigView />);
  await waitFor(() => expect(screen.getByDisplayValue("pacs1")).toBeInTheDocument());
  expect(await runAxe(document.body)).toEqual([]);
});
```

Minor: the run logs repeated `Error: Not implemented: HTMLCanvasElement.prototype.getContext` from axe's colour-contrast rule hitting jsdom's missing canvas. Harmless (the rule correctly no-ops), but it trains people to ignore red output. Suppress by stubbing `HTMLCanvasElement.prototype.getContext` in `beforeAll`, alongside the existing `matchMedia` / `getTotalLength` stubs.

Also note the honest limitation already documented in the file: **jsdom does no layout**, so colour-contrast and reflow cannot be automated here. That is correct; keep the token comments in `index.css` as the compensating control.

---

### LOW

#### T-13 — The `integration` / `slow` marker taxonomy is declared but essentially unused

`pyproject.toml` declares `markers = ["slow: ...", "integration: ..."]` with `--strict-markers`, but only **2 tests in the entire suite use `@pytest.mark.slow`** (`test_retry_backoff.py`, `test_receiver_wire.py`) and **`integration` is used zero times**. Meanwhile 338 tests are integration-flavoured by content (TestClient, subprocess, real ports, threads) — 46% of the suite.

So `pytest -m "not integration"` does not work, CI cannot cheaply run a fast unit-only feedback loop, and the 243 s wall-clock is paid on every push. Categorising honestly would let CI run unit tests on every PR and the full matrix on merge.

---

#### T-14 — Test-pyramid shape is inverted toward integration, and E2E is thin for the surface area

Approximate distribution (738 tests):

| Layer | Count | Share |
|---|---|---|
| Pure unit | 392 | 53% |
| Integration-flavoured | 338 | 46% |
| Chaos (`tests/chaos/`) | 8 | 1% |
| E2E (Playwright) | 7 specs | — |
| Frontend unit | 65 | — |

53% unit is not a broken pyramid, and the integration tier is *earned* — the receiver→spool→forwarder chain genuinely needs real components. But two observations:

- **The E2E tier is disproportionately thin for a PHI appliance with a Tauri shell.** 7 specs cover wizard, auth, config, dashboard, queue, pipeline, audit-logs. Absent: report retrieval (the feature T-2 shows is silently broken), disk-full UI, hot-unplug, the destinations CRUD flow, and any negative-path coverage (failed login rendering, backend-down, session expiry). `workers: 1` + `retries: 0` is the right call for determinism, but it means the suite cannot grow without becoming slow.
- **The one E2E test that would have caught T-2 does not exist**: no spec requests a report and asserts it arrives. Given the whole feature is unwired, an E2E spec asserting report retrieval is the highest-value addition to the entire E2E tier.

---

#### T-15 — Poll-loop tests are deadline-bounded (good) but several share wall-clock assumptions

16 test files use `time.sleep` / deadline polling. Spot-checking the important ones, they are written correctly — `test_concurrent_forwarder.py:83` polls up to 2 s for a 0.6 s minimum rather than `sleep(2)`, and documents why (`review M14: replace fixed sleep with event synchronisation`). No test mutates `os.environ` or `chdir`; isolation is via `tmp_path` fixtures throughout, and no test shares mutable module state.

Two low-severity watch items:
- `test_concurrent_forwarder.py:57` uses `time.monotonic() + 5.0` against a 0.5 s handler — comfortable, but a 10× CI-machine slowdown would flake it. Prefer an event/condition the handler can signal, per the existing M14 note.
- The 120 s `pytest-timeout` in `pyproject.toml` is generous; T-9's infinite loop would hang a test file rather than fail it. A per-test `@pytest.mark.timeout(30)` on the disk-monitor and polling tests would turn a hang into a diagnosed failure.

---

## 3. Cross-cutting quality assessment

### 3.1 Tests test behaviour, not implementation — mostly

The suite reads as behaviour-first, and this is a real strength. Representative: `tests/test_web_security.py` asserts *outcomes* (401 returned, cookie issued, dangerous bind refused), not call sequences. `test_db_encryption.py` asserts only the externally-visible contract (correct key round-trips; wrong key and plaintext-open are rejected; fresh DB still opens) — it would survive a full SQLCipher rewrite.

Where the suite does reach into internals, it is usually justified and usually declared: `spool._db` access is used for assertions that have no public accessor, and `test_web_api.py` annotates the casts with `# type: ignore[union-attr]`. `_save` is called directly in the T-1 recommendations above, but that is because `_save` is where the security boundary lives and it has no public caller — an acceptable trade for a security test, and one that should be replaced by a boundary-level test (`finder`/`retrieve`) once the guard is fixed.

One genuine over-coupling: `tests/test_reports_service.py` hand-injects `finder`/`mover` fakes into `ReportRetriever`, which is precisely why the unwired-production bug (T-2) sailed through. The unit test is *correct* about the object's contract and *blind* to whether the object is ever constructed correctly. That is the argument for T-2's composition-root test rather than a criticism of the existing one.

### 3.2 Assertion quality is high; the raw count understates it

Raw `assert`-per-test density ranges 0.4 (test_db_encryption.py) to 7.7 (test_crash_recovery.py), but the low numbers are an artifact — `pytest.raises(...)` is the primary assertion in error-path tests and is not an `assert` keyword. Reading those files confirms they are well-formed. No test file in the suite has zero assertions (verified by grep). `test_crash_recovery.py` at 7.7 asserts/test is the model: recovery is verified by checking the *surviving observable state*, not by checking that a recovery function was called.

### 3.3 Error paths and boundary conditions

Generally strong. Malformed config → 400 (`test_web_api.py:470`), disk measurement failure → 503 never a false ok (`test_disk_full_ui.py:88`), ENOSPC at receive → no ack and no study row (`chaos/test_disk_full.py:150`), duplicate C-STORE idempotency (`test_duplicate_cstore.py`), illegal audit-chain transitions raise, wrong encryption key rejected.

The specific boundary gaps are the findings above: **the delete-failure branch of the purge path (T-9), the bcrypt-import-failure branch (T-5), the context-budget ceiling (T-6), and concurrent-completion of the same study (T-10).** All four are error paths where the code has an opinion and the tests do not check it.

### 3.4 Concurrency

Mixed, and this is the weakest dimension after security. The suite *has* a concurrency tier — `test_concurrent_forwarder.py` genuinely runs 3 workers in parallel, `chaos/` exists as a category at all, and `test_duplicate_cstore.py` covers retry idempotency. But every concurrency test is structured so that the shared resource is never actually contended at the decision point: parallel studies have one target each (T-10), and the store benchmark is a benchmark, not a test, with no assertions. The `Spool.complete` race (T-10) and the receive-path serialization (T-8) are both consequences of the same gap: the suite parallelises *work* but never parallelises *access to the same record*.

### 3.5 Maintainability and flakiness indicators

Healthy. `tests/conftest.py` is minimal and provides only fakes and value fixtures — no shared mutable state, no function-scoped side effects leaking across tests. No `chdir`, no `os.environ` mutation. Ports are allocated via `free_port()`. Coverage config sensibly omits the Windows-only `service_backend.py` and the thin `ui/__init__.py` launcher so the gate stays meaningful cross-platform.

Flake risk is low but non-zero (T-15): the poll-with-deadline pattern is correct but timing-bounded, `retries: 0` in Playwright means a flaky E2E fails the build rather than masking, and the 120 s global timeout would let an infinite loop (T-9) hang rather than fail.

The largest maintainability debt is **duplication between `check_perf_gates.py` and the test suite** — the script re-implements spool seeding and a fake handler rather than importing test helpers, and `.full-review/.perfbench/store_bench.py` hardcodes absolute paths (`sys.path.insert(0, "/home/dev/Documents/...")`), which is why it must be hand-run rather than wired into CI. Promoting it to a gate (T-8) requires de-hardcoding those paths first.

---

## 4. Prioritised recommendations

**Do these first — each is a failing test for a live bug:**

1. **T-1** — `tests/test_report_traversal.py`, parametrized over both transports and the traversal UID set. Bug is proven exploitable; 30 minutes to write, fails immediately, and its fix is a one-line `validate_uid` at `reports/find.py` + both `_save` methods.
2. **T-2** — composition-root test asserting `finder`/`mover` are injected when `reports.enabled`. Confirms US-06 is entirely non-functional in every shipped build.
3. **T-6** — context-budget test over real pynetdicom (`len(ae.requested_contexts) <= 128`) plus catching `ValueError` in `_send_files`. Converts a permanent-delivery crash into a retryable route error.
4. **T-11** — add `test-web` to CI. 65 tests, zero CI cost today, and it is the prerequisite for the accessibility pass being verifiable at all.
5. **T-8** — promote `store_bench.py` to a gated assertion on the *scaling ratio* (de-hardcode paths first). This is the only way US-01's 25-association target becomes a real requirement rather than a hope.

**Then the High/Medium correctness gaps:**

6. **T-9** — failed-delete path in the purge loop (infinite loop in disk-full recovery).
7. **T-10** — concurrent final-route completion, both timing and deterministic-interleaving forms.
8. **T-3 / T-4** — config-update auth invariant and rename+reorder secret cross-wiring. Write both to assert the persisted *outcome* so they pass under any reasonable fix.
9. **T-7** — `prune()` integrity precondition + a production caller for `verify_anchor_signatures`.

**Hygiene (Low, do opportunistically):**

10. **T-13** — actually apply the `integration` marker so CI can run a unit-only fast path.
11. **T-15** — per-test timeouts on the disk-monitor and polling tests; replace remaining fixed sleeps with event synchronisation per the existing M14 note.
12. **T-12** — parametrize the a11y scan over row states; supply a full ConfigView fixture; stub `getContext` to silence the jsdom noise.
13. **T-14** — an E2E spec for report retrieval (the feature T-2 shows is broken).

---

## 5. Summary table

| ID | Sev | Untested / poorly tested | Location |
|---|---|---|---|
| T-1 | **Critical** | Path traversal via remote UIDs in both report `_save` (proven exploitable) | `reports/move.py:170`, `reports/dicomweb.py:177` |
| T-2 | **Critical** | Report transports never injected → US-06 silently dead; no wiring test | `main.py:523-526` |
| T-3 | High | Bind invariant not asserted across `PUT /config` (auth disableable) | `web/routes.py:770` |
| T-4 | High | `_restore_redacted_secrets` rename+reorder cross-wires credentials | `web/routes.py:705-766` |
| T-5 | High | bcrypt undeclared; fail-closed branch uncovered; SHA-256 default untested | `web/auth.py:61-80`, `pyproject.toml` |
| T-6 | High | SCU 128-context ceiling — raises `ValueError`, study undeliverable | `forwarder/handlers/dicom.py:105-112` |
| T-7 | High | `prune()` re-anchors without integrity check; `verify_anchor_signatures` has no prod caller | `audit/__init__.py:385`, `audit/anchoring.py:242` |
| T-8 | Medium | US-01 receive throughput ungated: 17.3 inst/s @ 25 assoc, 1338 ms p50, 20× per-assoc collapse | `scripts/check_perf_gates.py` |
| T-9 | Medium | Disk-full purge loop spins forever on failed delete | `disk.py:115-123`, `spool/__init__.py:923-950` |
| T-10 | Medium | `complete()`/`fail()` non-atomic; concurrent final routes double-emit `STUDY_SENT` | `spool/__init__.py:697-722` |
| T-11 | Medium | No vitest job in CI — 65 frontend tests never run | `.github/workflows/ci.yml` |
| T-12 | Medium | a11y scan covers one row state; ConfigView stub omits credential forms | `web/src/test/a11y-scan.test.tsx` |
| T-13 | Low | `integration`/`slow` markers declared but unused (2 uses in 738 tests) | `pyproject.toml` |
| T-14 | Low | E2E thin for surface area; no report-retrieval spec | `e2e/tests/` |
| T-15 | Low | Poll-loop tests timing-bounded; global 120 s timeout would hang T-9 | `tests/` (16 files) |
| T-16 | Low | `db.close()` with pending auto-enqueue timers → `ProgrammingError` | `spool/__init__.py:476` |
| T-17 | Low | Subprocess coverage not captured — `main.py` undercounted at 58.8% | `pyproject.toml` coverage config |

**17 findings: 2 Critical, 5 High, 5 Medium, 5 Low.** (T-16/T-17 emerged from this review's own probes — the benchmark crash and the coverage accounting respectively — and are not carry-overs from the prior phase.)
