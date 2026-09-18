# Phase 2-2B — Performance & Scalability Analysis

**Repository:** dicom-gateway (Python/FastAPI + React/TS admin panel, Tauri-wrapped appliance)
**Base commit:** `aab94b5` + uncommitted WIP in `web/`
**Scope:** receive hot path (C-STORE SCP, US-01: 25 concurrent associations), forwarding path (per-study delivery + retry), admin API, reports retrieval, frontend bundle/render.

---

## 0. Executive summary — the receive-path ceiling, measured

The single most important fact in this review is empirical, not speculative. I benchmarked the actual
`Spool.store_instance` path on this machine's real ext4 volume (`/dev/sda2`, not tmpfs) at 1 and at 25
concurrent threads, using the production `Spool` + `Database` objects and real pydicom datasets.

| Concurrency | Total throughput | Per association | Per-instance latency (median) | p95 | max |
|---|---|---|---|---|---|
| 1 association | **21.4 inst/s** | 21.4 inst/s | 41 ms | 56 ms | 770 ms |
| 25 associations (US-01 target) | **31.3 inst/s** | **1.25 inst/s** | **718 ms** | 1 428 ms | 3 223 ms |

**Interpretation.** Going from 1 to 25 concurrent associations buys only **1.46×** throughput while
per-instance latency grows **17.5×** (41 ms → 718 ms). That is the textbook signature of a fully
serialized critical section: the 25 association threads spend 94% of their time queued on one lock, and
throughput saturates at ~1.5× the single-thread rate regardless of how many associations are allowed.

**Where the time goes (per instance, on this disk):**

| Barrier | Location | Measured cost |
|---|---|---|
| 1 × instance-file `fsync` | `spool/__init__.py:353` → `_fsync_instance` | ~10.0 ms (median 9.9, p95 14.5) |
| 1 × `insert_instance_meta` FULL commit | `spool/db.py:1035` | ~8.5 ms |
| 1 × `upsert_study_instance` FULL commit | `spool/db.py:421` | ~8.5 ms |
| **Total serialized critical section** | | **~27 ms → ceiling ≈ 37 inst/s** (measured 31) |

Both DB commits and the file fsync happen *inside the single process-wide `RLock`* on the single
shared connection (`spool/db.py:190`), so they cannot overlap with each other or across associations.

**Demand vs. supply.** 25 modalities pushing at even a modest 10 images/s each = **250 inst/s demanded
against ~31 inst/s supplied**. The backlog grows unboundedly at ~220 inst/s, and the 718 ms median
C-STORE response time is what the modality experiences. Modalities that re-send after an association
timeout convert the backlog into duplicate instances, compounding the problem.

**The perf gates do not cover this.** `scripts/check_perf_gates.py` measures two things: (a) forwarding
latency from claim to first delivery attempt (budget 2 s) and (b) a **5 items/s** forwarding throughput
floor against a *scripted fake handler* (`check_perf_gates.py:35-41`) that does no I/O. Neither touches
the receive path. The US-01 acceptance criterion — 25 concurrent associations — is asserted only as a
config default (`config/__init__.py:85`, `max_associations=25`); sustained receive throughput under
concurrency is **unmeasured and ungated**. A regression that halved receive throughput would pass CI
green.

**The durability setting is correct and must not be weakened.** `synchronous = FULL` under WAL is the
right choice (NORMAL would skip the COMMIT fsync and lose an already-acknowledged transaction on power
loss; `db.py:203-208` documents this correctly). The fix is to reduce the **number** of fsync barriers
per instance and to let them overlap across associations — not to weaken any single one.

---

## Findings by area

### A. Database performance

#### A-1. High — the receive hot path performs two separate FULL-fsync commits per instance

**Files:** `src/mercure_gateway/spool/__init__.py:340-363`, `src/mercure_gateway/spool/db.py:1035` & `db.py:421`

`store_instance` writes `instance_meta` inside `_apply_transfer_syntax` (`__init__.py:568`) and then,
in a *separate* `transaction()`, upserts the study row (`__init__.py:355`). Each `transaction()` is its
own `BEGIN IMMEDIATE` + `COMMIT`, and `COMMIT` under `synchronous=FULL` fsyncs the WAL — so each
instance pays **two** WAL fsyncs (~17 ms measured) where one would do.

**Impact:** ~7 ms of the ~27 ms serialized critical section — **26% of receive-path time**, and it is
purely structural: the two writes are logically independent and could share one transaction, or the
instance-meta write could be moved off the synchronous pre-ack path entirely (it is provenance data,
already best-effort in its error handling at `__init__.py:577-579`).

**Recommendation (highest value-per-line change in the codebase):**

```python
# spool/db.py — one new method, one transaction for both writes
def upsert_study_with_instance_meta(
    self, study_uid, accession, mrn, patient_name, modality,
    *, new_instance, new_series,
    series_uid, instance_uid, file_path, received_syntax, stored_syntax, num_bytes,
) -> int:
    with self.transaction() as conn:
        conn.execute("INSERT INTO studies (...) VALUES (...) ON CONFLICT(study_uid) DO UPDATE SET ...",
                     (...))
        conn.execute("INSERT INTO instance_meta (...) VALUES (...) ON CONFLICT(instance_uid) DO UPDATE SET ...",
                     (...))
        row = conn.execute("SELECT id FROM studies WHERE study_uid = ?", (study_uid,)).fetchone()
    return int(row["id"])
```

Measured on this disk: 2 statements/1 txn = **10.1 ms** vs 2 × 1-statement txns = **16.9 ms**. That
lifts the ceiling from ~31 to ~45 inst/s on this volume (+45%) with zero durability loss — both writes
are still inside one atomic `BEGIN IMMEDIATE`.

**Bigger structural fix (see A-2):** move `instance_meta` off the pre-ack critical path entirely by
queueing it onto a batched background flush, since the code already tolerates its loss
(`__init__.py:577-579` logs and continues).

#### A-2. High — one connection + one RLock serializes receiver, forwarder, web, and report poller

**File:** `src/mercure_gateway/spool/db.py:180-211`

One `sqlite3.Connection` (`db.py:192`) guarded by one `threading.RLock` (`db.py:190`) is shared by:
up to 25 association reactor threads (receiver), 3 forwarder workers (`config/__init__.py:376`,
`concurrency=3`), the FastAPI thread pool (every admin API call), the disk monitor, the report poller,
and the hub streamer's outbox writes. Every read takes `self._lock` (`db.py:464`, `:506`, `:569`, …).

**Impact:** this is the mechanism behind the measured 1.46× scaling factor. It also means any slow
admin query (see A-4, A-5, B-2) directly stalls DICOM ingestion — the US-10 isolation invariant
("a stuck filesystem can never block receive/forward", `disk.py:11`) is not upheld at the DB layer,
only at the disk layer.

**Recommendation — two independent steps, in order:**

1. **A read-only second connection on the same WAL file.** WAL readers never block the writer and do
   not need `BEGIN IMMEDIATE`, so all admin reads (`get_study`, `list_studies*`, `count_*`,
   `list_audit*`, `spool_num_bytes`) can drop the process-wide lock entirely:
   ```python
   def __init__(self, path, *, encrypt_key=None):
       self._write_lock = threading.RLock()
       self._conn = self._connect(path)                 # writer (receiver/forwarder)
       self._read_conn = self._connect(path)            # reader (web/admin), lock-free
   ```
   (Same-thread `check_same_thread=False` is already set.) WAL gives a consistent snapshot per read
   transaction; the admin panel tolerates snapshot staleness of a few ms.
2. **Batch the receive-path writes.** Decouple "ack" from "row visible": ack after the *file* fsync +
   the study upsert (one txn), and flush `instance_meta` rows in batches from a background thread.
   This converts per-instance DB cost from 2 fsyncs to ~0.

#### A-3. High — `_requeue_complete_routes` issues one FULL-fsync transaction per destination

**File:** `src/mercure_gateway/spool/__init__.py:422-440`

When a *duplicate-or-late* instance arrives for a study that is already routed and `complete`, the
receive path loops over routes and calls `reset_route_waiting(route["id"])` (`__init__.py:434`) — each
one its own `transaction()` (`db.py:847-854`). With D destinations this adds **D × ~8.5 ms** of
serialized lock time to a single C-STORE.

**Impact:** for a 3-destination gateway the receive path on a re-opened study costs ~27 ms + 25 ms =
**~52 ms** instead of 27 ms. Worse, `get_routes` (`db.py:428`) and `get_study` (`db.py:429`) are two
extra round trips under the same lock. Modalities that re-send after a timeout hit this path
constantly — the codebase explicitly anticipates re-sends (`db.py:412-419`).

**Recommendation:** make it one statement:
```python
self._conn.executemany(
    "UPDATE task_routing SET status='waiting', updated_at=CURRENT_TIMESTAMP "
    "WHERE study_id=? AND status='complete'",
    [(study_id,)],
)
```
(single statement, no per-route loop), and fold the two guard reads into the same transaction.

#### A-4. High — `/api/pipeline` materializes the entire studies table on every poll

**Files:** `src/mercure_gateway/web/pipeline.py:142-145` → `spool/db.py:572-605`

`received_hour` is computed by calling `list_studies_with_route_counts()` **with no `limit`** and
counting rows in Python. Every `/api/pipeline` request therefore runs a full
`studies ⋈ task_routing GROUP BY s.id` over the whole table, fetches every row, converts each to a
`dict` (`spool/__init__.py:172-177`), and discards them. The dashboard polls this endpoint every 2 s
(frontend F-4.1).

**Impact:** on a 100 k-study gateway this is a full table + join + group + 100 k-row materialization
per poll — hundreds of ms of DB time, holding the same lock the receiver needs (A-2). The predicate
`created_at >= ?` is already indexed (`idx_studies_created_at`, `db.py:123`), so SQL answers it in
microseconds.

**Recommendation:**
```python
# spool/db.py
def count_studies_since(self, since: str) -> int:
    with self._lock:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM studies WHERE created_at >= ?", (since,)
        ).fetchone()
    return int(row["n"]) if row else 0
```

#### A-5. High — `count_routes_by_target()` is an unbounded GROUP BY scan, uncached

**File:** `spool/db.py:674-688`, called from `web/pipeline.py:128`

```sql
SELECT target_name, target_type, status, COUNT(*) AS n, MAX(updated_at) AS last_activity
FROM task_routing GROUP BY target_name, target_type, status
```
No `WHERE`, no index on `(target_name, target_type, status)`. `task_routing` grows as
studies × destinations, so a 100 k-study / 3-destination gateway = 300 k rows scanned and grouped per
poll, under the shared lock. There is **no caching anywhere in the web layer** (the one exception is
`DestinationHealthMonitor`, `web/pipeline.py:51-96`).

**Recommendation:** add a covering index so the group-by is answered from the index alone, plus a
short-TTL memo (the dashboard polls at 2 s; 5 s staleness is invisible):
```sql
CREATE INDEX IF NOT EXISTS idx_task_routing_target
    ON task_routing(target_name, target_type, status, updated_at);
```

#### A-6. Medium — `spool_num_bytes()` is a full-table SUM, called in a purge loop

**Files:** `spool/db.py:989-1006` (query), `disk.py:138, 146, 150` (loop callers)

```sql
SELECT COALESCE(SUM(m.num_bytes), 0) FROM instance_meta AS m JOIN studies AS s ON s.study_uid = m.study_uid
```
No `WHERE`, no covering index for `(study_uid, num_bytes)`. `DiskMonitor._enforce_spool_cap` calls it
**once per purge iteration** (`disk.py:146`) plus once inside the failure log message (`disk.py:150`),
so purging K studies costs **K+2 full scans** of the instance table, each holding the shared RLock and
each directly stalling the receive path (A-2).

**Recommendation:** `CREATE INDEX idx_instance_meta_bytes ON instance_meta(study_uid, num_bytes);`
plus compute the total once and subtract purged bytes, or only recompute every N iterations.

#### A-7. Medium — audit export/verify are unbounded and lock-holding

**Files:** `web/routes.py:1056-1084` (export, limit up to 100 000), `web/routes.py:1044-1053` →
`audit/__init__.py:278-309` (verify: `SELECT ... ORDER BY id` with **no LIMIT**, then SHA-256 of every
row)

Both run under the shared lock; `/audit/verify` on a gateway with millions of events is a multi-second
full-table scan + per-row hashing triggered by a single HTTP GET. The codebase already knows this —
`routes.py:248-251` excludes chain verification from `/system/metrics` for exactly this reason.

**Recommendation:** bound the replay to a recent window (`LIMIT`), cap the error list, short-TTL cache
the result (a tampered chain cannot repair itself between requests), and move the per-row
`json.loads`/`redact_phi` out of the lock scope in the export path (`iter_audit_events` already exists
at `db.py:1109-1115` for streaming).

#### A-8. Positive — no N+1 in the study endpoints

`/studies` is a single joined + paginated query (`routes.py:517-519`); `/studies/{id}`, `/detail`,
`/routes` each issue a constant 2 queries. The only per-item DB loop in the web layer is
`recovery.py:180` (`get_routes` inside a study loop) — startup-only, Low. `enqueue_study` calls
`get_routes` twice back-to-back (`spool/__init__.py:783-787`) — redundant but harmless.

---

### B. Receive path / fsync amplification

#### B-1. Critical — the 128 presentation-context fallback is dead code that makes studies permanently undeliverable

**Files:** `src/mercure_gateway/forwarder/handlers/dicom.py:105-112`, `sop_classes.py:280-300`

I verified this by executing the code path. `STORAGE_SOP_CLASSES` has **111** entries. The fallback
branch (`contexts = list(sop_classes_for_files(files)) or list(_STORAGE_CONTEXTS)`) requests
**111 × 4 = 444** contexts. pynetdicom's cap is hard-coded at 128
(`pynetdicom/ae.py:256`: `if len(self.requested_contexts) >= 128: raise ValueError`), and adding the
129th raises:

```
ValueError: Failed to add the requested presentation context as there are already
the maximum allowed number of requested contexts
```

`sop_classes_for_files` (`sop_classes.py:294-297`) **silently skips unreadable files**, so this branch
fires whenever a study's headers are all unreadable (truncated/corrupt receive, a proprietary SOP class
outside the 111) — exactly the degraded case the fallback was written to rescue. The docstring at
`dicom.py:102-104` acknowledges the 444-context problem but the fallback is the thing that triggers it.

The `ValueError` is caught by `Forwarder._dispatch`'s blanket `except Exception`
(`forwarder/__init__.py:223`) and converted into a delivery failure, so the study errors through the
state machine, retries 5 times with backoff, and lands **FAILED** — permanently undeliverable, with an
error message that names neither the real cause nor the study.

Second-order: even for a *readable* study, > 32 distinct SOP classes × 4 contexts = 128 also trips the
cap. Rare for a single study, but not impossible for a mixed SR/PDF/waveform study.

**Recommendation:**
```python
# Cap the request list, and fall back to the ALL-SYNTAXES form (1 context/class),
# which is the only fallback that actually fits:
contexts = list(sop_classes_for_files(files)) or list(_STORAGE_CONTEXTS)
for ctx in contexts[:31]:                      # 31 × 4 = 124, leaves room
    ae.add_requested_context(ctx, AllTransferSyntaxes)
    for syntax in _COMPRESSED_SYNTAXES:
        ae.add_requested_context(ctx, syntax)
```
Add a unit test asserting `len(ae.requested_contexts) <= 128` for both the normal and the
fallback branch — there is already a `test_sop_class_budget` for the SCP side; the SCU side has none.
And surface the real failure: `_dispatch` should let a `ValueError` from context negotiation be
reported distinctly rather than as a generic handler crash.

#### B-2. High — per-instance fsync count is 3 and cannot overlap across associations

**Files:** `spool/__init__.py:353` (file fsync + up to 3 directory fsyncs), `db.py:1035`, `db.py:421`

Quantified in §0. Directory fsyncs are effectively free on this disk (~0.001 ms, measured); the cost is
1 file fsync (~10 ms) + 2 WAL commits (~8.5 ms each). All three execute while holding the RLock.

Note the `.tags` sidecar (`spool/tags.py`, written at `__init__.py:563`) is deliberately *not* fsynced
(`__init__.py:396-397`) — correct, it is derived data.

**Recommendation:** as A-1 (merge the two commits → ~10 ms saved/instance) and A-2 (a second
read connection so admin reads stop competing). A further option, safe under the store-before-ack
contract: keep the file fsync + study upsert on the ack path and defer `instance_meta` to a batched
background flush, cutting the DB cost to 1 fsync (~8.5 ms) and the critical section to ~19 ms
→ ~52 inst/s on this volume.

#### B-3. Medium — C-STORE handler runs synchronously in the pynetdicom reactor thread

**File:** `src/mercure_gateway/receiver/__init__.py:118-134`

`_on_c_store` does its work inline in the association's reactor thread, so a 718 ms store blocks *all*
PDU handling for that association — including consuming the next PDU, which is what produces the
association-level timeouts that modalities respond to by re-sending (duplicates, which then hit A-3).
This is correct for the store-before-ack contract (the status must reflect the persist result), so the
fix is to make `store_instance` fast (A-1/A-2/B-2), not to move it off the reactor.

**Impact:** today the 25-association case gives each modality a 718 ms median ack latency (measured);
a CT study of 600 instances takes ~8 minutes to receive from one modality at 1.25 inst/s.

#### B-4. Medium — `check_perf_gates.py` measures neither the receive path nor real I/O

**File:** `scripts/check_perf_gates.py` (whole file)

Gates: forwarding latency ≤ 2 s (`:21`) and throughput ≥ **5 items/s** (`:22`) against
`_StopwatchHandler.deliver`, which returns instantly (`:41`) and writes nothing. The 5 items/s floor is
~1/6 of the measured ~31 inst/s receive ceiling, so even a severe receive regression would not trip it,
and the fake handler means no fsync, no DB write, and no socket is ever exercised.

**Recommendation:** add a third gate that measures sustained `store_instance` throughput at
`max_associations` concurrency against a real on-disk database, with a floor derived from the US-01
target (e.g. ≥ 25 inst/s at 25 associations, and assert per-instance p95 ≤ 500 ms). Benchmarks used
for this report are preserved in `.full-review/.perfbench/` as a starting point.

---

### C. Forwarding path

#### C-1. High — no association/transport timeouts; a hung destination pins a worker and its route

**Files:** `forwarder/handlers/dicom.py:73-77` and `:114`, `forwarder/handlers/sftp.py:103-115`

- `ae.associate(host, port, ae_title=...)` passes **no `timeout`** — an unresponsive PACS blocks the
  worker indefinitely. `Forwarder.stop()` joins with a 5 s timeout and abandons the route
  (`forwarder/__init__.py:151-165`), but there is no per-delivery deadline during normal operation.
- **SFTP `connect()` has no `timeout` argument at all** (`sftp.py:103-108`, `:110-115`) — compare
  `dicomweb.py:50` (`timeout=60`) and `rsync.py:49` (`timeout=300`), which do set one. A hung SSH server
  pins a forwarder worker and its route forever; with `concurrency=3` (`config/__init__.py:376`), three
  hung destinations stall the entire queue.

**Impact:** with only 3 workers, one unreachable destination can reduce forwarding capacity by 33% and
there is no upper bound on how long the route stays in `sending`.

**Recommendation:**
```python
# sftp.py
client.connect(hostname=..., port=..., username=..., password=password,
               timeout=self.destination.timeout_sec or 30)
# dicom.py
assoc = self._open_association(ae)   # pass timeout= to associate()
```
Add a `timeout_sec` field to `SFTPDestination`/`DICOMDestination` with a sane default, and bound
`_dispatch` with a per-delivery deadline so a stuck transport fails the route into `error` where the
operator can see and re-forward it.

#### C-2. Medium — every delivery reads each DICOM file twice

**Files:** `forwarder/handlers/dicom.py:105` → `sop_classes.py:294` (header read), then `dicom.py:120`
(`pydicom.dcmread(str(path), stop_before_pixels=False)` in the send loop)

`sop_classes_for_files` opens and parses every file header to build the context list, then the send loop
re-reads every file in full. `Spool.study_files` (`spool/__init__.py:264`) also does a sorted `rglob`
over the study tree per delivery. For a 600-instance study that is 1 200 file opens + 1 rglob + a sort
per delivery, and it all happens while the route is claimed in `sending`.

**Recommendation:** `sop_classes_for_files` could return `(class, transfer_syntax)` pairs so the send
loop can skip its own header parse, or the context list can be derived from `instance_meta` rows
(`spool/db.py:1008-1018`, already indexed by `study_uid`) which the DB has from receive time — no file
I/O at all for the common case.

#### C-3. Medium — `Spool.complete`/`fail` are not atomic across mark/read/promote

**Files:** `spool/__init__.py:697-721` (`complete`), `:723-752` (`fail`)

`complete()` performs `mark_route_sent` (txn) → `all_routes_complete` (read) → `set_study_state` +
`set_retention_delivered` (2 more txns). Two forwarder workers finishing the last two routes of a study
concurrently can both read `all_routes_complete() == True` and both promote, double-emitting the
`STUDY_SENT` audit event and double-stamping `retention_delivered_at`. Each call also issues a separate
`get_routes` round trip.

**Impact:** correctness more than raw speed, but each extra transaction is ~8.5 ms of lock time on the
forwarding path, and the double-audit-event feeds the hub outbox.

**Recommendation:** promote inside one statement with a guard:
```python
UPDATE studies SET state='SENT', retention_delivered_at=CURRENT_TIMESTAMP
WHERE id=? AND state='SENDING'
  AND (SELECT COUNT(*) FROM task_routing WHERE study_id=? AND status!='complete') = 0
```
and only emit `STUDY_SENT` when the write affected a row.

#### C-4. Low — a new association is opened per study (no pooling)

**File:** `forwarder/handlers/dicom.py:97-114` (`_send_files` builds a fresh `AE` + association per study)

Each delivery pays a TCP connect + ACSE handshake + context negotiation. Correct for store-and-forward
semantics (one association per task keeps failure boundaries clean), and the retry design already
avoids holding queue slots during backoff (`forwarder/__init__.py:240-248`). Noting it as a known,
deliberate trade-off — if queue latency ever dominates, an idle-keepalive association pool per
destination is the lever.

---

### D. Disk monitor / availability

#### D-1. Critical — the disk-full purge loop spins at 100% CPU forever when a delete fails

**Files:** `disk.py:115-123`, `spool/__init__.py:905-917`

```python
while pct >= self._warning_pct:                      # disk.py:115
    if not self._spool.purge_oldest_delivered():     # :116
        ... break
```
`purge_oldest_delivered()` returns `True` whenever a row was *selected*, but `_purge_study_dir`
(`__init__.py:923-943`) **returns early without deleting the row** when `shutil.rmtree` fails
(`__init__.py:936-942`) — e.g. a locked file, a read-only USB stick, a Windows handle. The next
iteration re-selects the same oldest study, fails the same way, and the loop spins **with no sleep**,
on a disk that is already failing, pegging a core and generating unbounded error logs.

The `_enforce_spool_cap` loop (`disk.py:146-152`) has the identical shape but is partially shielded
because `spool_num_bytes()` changes only when rows delete — so it *does* break when deletes fail. The
disk-percentage loop has no such shield.

**Impact:** availability + perf. A gateway on a read-only or failing volume enters a hard spin loop
that competes for CPU with the receive path and never recovers, and the disk never frees space.

**Recommendation:** return a tri-state from the purge, and always sleep between attempts:
```python
# spool/__init__.py
def purge_oldest_delivered(self) -> str:  # "purged" | "none_eligible" | "delete_failed"
    row = self._db.list_oldest_delivered()
    if row is None:
        return "none_eligible"
    if not self._purge_study_dir(raw_uid=..., study_id=...):
        return "delete_failed"          # caller backs off; same row will fail again
    return "purged"
```
```python
# disk.py — break on failure, not just on emptiness, and never spin
if result in ("none_eligible", "delete_failed"):
    if result == "delete_failed":
        logger.error("purge failed; backing off %.0fs", self._poll_sec)
    break
```

#### D-2. Low — `_requested_at` grows for process lifetime (reports)

**File:** `reports/__init__.py:83, 176` — one entry per report ever requested, never evicted. Bounded by
the number of reports in practice; evict on terminal status.

---

### E. Memory management

#### E-1. High — reports retrieve buffers the entire study's datasets in RAM

**Files:** `reports/move.py:99-106` (handler), `:140-141` (retention), `:146-158` (save)

```python
received: dict[str, Any] = {}
def on_c_store(event):
    with received_lock:
        received[str(ds.SOPInstanceUID)] = ds      # every dataset object retained
```
Because the C-MOVE is STUDY-level (F-M8 below), the store SCP receives the **whole study**; every
dataset object is held until the association ends, and only then written to disk. A 600-instance 1 GiB
study fully materializes in heap for the duration of the retrieve.

**Recommendation:** persist in the C-STORE handler and keep only a UID set:
```python
def on_c_store(event) -> int:
    ds = event.dataset
    uid, cls = str(getattr(ds, "SOPInstanceUID", "")), str(getattr(ds, "SOPClassUID", ""))
    if cls in (SR_SOP_CLASS, PDF_SOP_CLASS):
        self._save(ds, str(getattr(ds, "StudyInstanceUID", "")), cls, uid)
    return 0x0000
```

#### E-2. Medium — reports C-MOVE is issued once per matched instance, each moving the whole study

**Files:** `reports/move.py:130-138` (loop), `:161-168` (STUDY-level query dataset)

`_move_dataset` sets only `QueryRetrieveLevel="STUDY"` + `StudyInstanceUID`; the `series_uid`/
`sop_instance_uid` carried by every `ReportMatch` are discarded. The loop iterates per match, so N
matched report instances produce N identical full-study C-MOVEs against a clinical PACS. The C-FIND
runs at IMAGE level with a universal `SOPClassUID=""` match (`find.py:98-106`), so a typical study
(600 CT images + 1 SR + 1 PDF) yields `matches=2` → **2 full-study moves ≈ 1 202 instances transferred
instead of ~2**. Wire cost scales as `matches × study_size`; the moves are strictly serial, so wall
time multiplies too. `report_type="both"` doubles it again (`reports/__init__.py:166-187`).

**Recommendation:**
```python
seen: set[str] = set()
for match in matches:
    if match.study_uid in seen:
        continue
    seen.add(match.study_uid)
    ds = Dataset(); ds.QueryRetrieveLevel = "STUDY"; ds.StudyInstanceUID = match.study_uid
    for status, _ in assoc.send_c_move(ds, self.store_scp_ae_title, _STUDY_ROOT_MOVE): ...
```
Grouping by study (not by instance) is the safe fix — many PACSes accept Study Root moves only at
STUDY level.

#### E-3. Medium — C-FIND enumerates the entire study; no early stop

**File:** `reports/find.py:118-141`

The IMAGE-level universal query makes the PACS enumerate all instances of the study with the SR/PDF
filter applied client-side. C-FIND duration and round trips are O(study instance count) even when only
1-2 reports exist, and there is no early `break` despite `_do_retrieve` only ever using `retrieved[0]`
(`reports/__init__.py:245`).

**Recommendation:** a SERIES-level pre-query with `Modality="SR"` (server-side match key) reduces the
candidate set to O(#SR series); add `first_only=True` and break on the first wanted SOP class.

#### E-4. Low — bounded collections are correctly bounded (positive)

`HubEventStreamer` uses a bounded `deque(maxlen=1000)` + a durable `hub_outbox` table with an explicit
row cap (`hub_events.py:43-93`, `db.py:1130-1150`). The receiver has no per-association buffering.
`_enqueue_timers` is keyed by study and cancelled on shutdown (`spool/__init__.py:244-250`). No
unbounded in-memory growth on the receive or forwarding paths.

---

### F. Frontend (`web/`)

The frontend was analyzed separately and in depth; the headline items:

#### F-1. Critical — 1.3 MB of test-only accessibility scanner is shipped and served publicly

**Files:** `web/public/axe.js` (1 305 279 B, untracked), duplicated at
`src/mercure_gateway/web/static/axe.js` (byte-identical, md5 `988d572c30bf870974a099db8ef1d90a`)

Vite copies `public/` verbatim into `outDir` (`web/vite.config.ts:14`), and FastAPI serves that whole
directory via `app.mount("/", StaticFiles(...), name="spa")` (`web/__init__.py:201`) — which is added
*after* the `require_auth` router include, so the static mount is **not authenticated**.
**Nothing ever loads it**: the only consumer is `web/src/test/a11y-scan.test.tsx:2`, which imports
`axe-core` from `node_modules`. `axe-core` is correctly a `devDependency` (`package.json:32`).

**Impact:** adds 1 305 279 raw / **240 183 gzip bytes** to the served payload — a **6.1× increase** over
the entire app payload (app JS+CSS = 66 KB gzip). Zero benefit. `.gitignore:24-25` covers
`static/assets/` and `static/index.html` but **not** `static/axe.js`, so the next `git add -A` commits
1.3 MB into the repo (compare `static/favicon.svg`, already tracked).

**Recommendation:** delete `web/public/axe.js` and the stale `src/mercure_gateway/web/static/axe.js`;
add `/axe.js` to `.gitignore`. The a11y test keeps working — it imports from `node_modules`.

#### F-2. High — no route-level lazy loading; all 9 pages eagerly parsed

**File:** `web/src/App.tsx:3-12` (static imports), `:264-272` (single ErrorBoundary switch)

Exactly one page is ever mounted, but all nine are parsed and evaluated on every cold start. The only
dynamic `import()` in the codebase is the Tauri plugin load in `ui/UpdaterBanner.tsx:24,46` (correctly
gated). `DestinationsView` alone is 24 KB of source. No `manualChunks` in `web/vite.config.ts:11-26`
either, so app changes also invalidate the framework cache (single 200 KB chunk, 62 KB gzip).

**Recommendation:** `React.lazy(() => import(...))` per page + a `<Suspense>` wrapper inside the
existing `ErrorBoundary key={page}` at `App.tsx:263`; split `react`/`react-dom` into a vendor chunk.

#### F-3. High — polling re-renders the whole SVG diagram every 2 s, with no request dedup or cancellation

**Files:** `web/src/pages/PipelineView.tsx:19,34,61-65` (2 s poll), `LogsView.tsx:8,27-31` (5 s poll)

- Both pollers use bare `setInterval(load, ms)` — `setInterval` does not await its callback, so if the
  backend is slow (the app ships a dedicated `BackendDownView` for exactly this case) requests
  **stack without bound**, and stale responses can land last and overwrite fresh ones (last-write-wins,
  no ordering guard).
- **Zero `AbortController` and zero `document.hidden` gating** anywhere in `web/src` — polls continue
  forever while the window is hidden (the Tauri shell minimizes to a tray), and unmounting resolves
  into state on a dead component.
- `setSnap(s)` is called unconditionally every tick with a freshly-parsed object, so the entire
  fixed-geometry SVG diagram re-renders every 2 s; `ui/flow.tsx` children are plain function components
  (zero `React.memo` in the codebase) receiving freshly-allocated array-literal props
  (`PipelineView.tsx:149-161`).

**Impact:** ~30 requests/minute (pipeline) + 12/minute (logs) before any user interaction, against an
admin API on the same process as DICOM forwarding — and each poll hits the unbounded queries A-4/A-5.

**Recommendation:** self-scheduling `setTimeout` that awaits the previous fetch; pass `signal` through
`apiFetch` (`web/src/api.ts:94`); gate on `visibilitychange`; add an equality guard (or ETag /
`If-None-Match` in `api.ts:101-105`) before `setSnap`. Centralize in a shared `useFetch` hook (F-4).

#### F-4. Medium — ~150-200 lines of duplicated fetch boilerplate across 9 components

**Files:** `QueueView.tsx:27-47`, `AuditView.tsx:7-24`, `ReportsView.tsx:14-30`, `LogsView.tsx:5-31`,
`PipelineView.tsx:32-65`, `ConfigView.tsx:8-33`, `DestinationsView.tsx:187-222`,
`ui/ServiceCard.tsx:20-31`, `App.tsx:30-38` — no shared `useAsync`/`useFetch`/`usePoll` hook exists
(grep returns zero hits).

Not a runtime cost — a maintenance cost that directly blocks fixing F-3, since every fix would
otherwise be patched in 9 places.

#### F-5. Medium — `AuthContext` value is not memoized

**File:** `web/src/context/AuthContext.tsx:95` — inline object literal; `checkAuth`/`login`/`logout`
at `:35`, `:59`, `:86` are plain function declarations re-created every render. Any auth state flip
re-renders the entire authenticated tree (consumers include `App.tsx:167`). Bounded impact today (auth
changes are rare) but defeats `React.memo` on every consumer. Fix: `useCallback` the handlers +
`useMemo` the value.

#### F-6. Medium — `PipelineView` destination table is unbounded and re-polled every 4 s

**File:** `web/src/pages/PipelineView.tsx:224-247` — maps over `destStudies` with no limit/pagination/
windowing; `api.ts:252-254` passes no cap; the selection poll re-fetches every 4 s
(`PipelineView.tsx:67-89`). A destination that has routed 10 000 studies renders 10 000 `<tr>` rows,
each with an inline `onKeyDown` closure. `AuditView`/`ReportsView` are capped at a hardcoded 200
(`AuditView.tsx:14`, `ReportsView.tsx:20`) with no load-more path; `QueueView` is correctly paginated
(`PAGE_SIZE = 50`, `:6`, `:134-146`); `LogsView` correctly joins into one `<pre>` (`:60`).

#### F-7. Low — WIP diff is a11y-only, no perf regressions

`git diff web/src` (11 files, +109/-18) is entirely accessibility work (skip link, `role="alert"`,
keyboard parity, focus-ring tokens). The only perf-relevant byproduct is the `axe-core` devDependency
addition (`package.json:32`) that introduced the vendored `axe.js` (F-1), plus one inline `onKeyDown`
closure per `PipeNode` on the 2 s-polling page (`ui/flow.tsx:35-50`) that would defeat a future
`React.memo`. Clean up F-1 alongside the commit.

---

### G. Scalability / horizontal barriers

#### G-1. High — stateful single-process design makes horizontal scaling impossible today

The gateway is deliberately a single-node appliance: the SQLite spool (`db.py:8-12` explicitly notes
`:memory:` databases cannot be shared across connections, hence one shared connection), the DICOM SCP
port binding, the on-disk spool under `storage.spool_dir`, the in-process forwarder worker pool, and
the file-based duplicate detection (`spool/__init__.py:331`, `path.exists()`) all assume a single
node with a single filesystem view.

**Impact:** the only scaling axis available is *vertical* (faster disk + fewer fsyncs), and the
measured ceiling (~31 inst/s on this volume) is a hard single-instance limit. Two gateway instances
against one PACS would double-ack and double-deliver (duplicate detection is filesystem-local, and
`upsert_study_instance`'s duplicate semantics depend on the file existing first — `__init__.py:331`).

**Recommendation:** this is an architecture boundary, not a bug. Document it, and make the numbers
visible: the perf gate (B-4) should report the measured receive ceiling per platform so capacity
planning does not rely on the config default (`max_associations=25`) being equivalent to *throughput*
at 25 associations. If multi-node ever becomes a requirement, the `hub_outbox` + bookkeeper
(`hub_events.py`, already at-least-once durable) is the right seam for coordinated claiming.

#### G-2. Low — single points of failure are already mitigated where it matters

The disk monitor is isolated on its own daemon thread with a documented isolation invariant
(`disk.py:10-12`); the forwarder's retry sleep is interruptible via `threading.Event`
(`forwarder/__init__.py:244`); the hub outbox survives restarts (`db.py:153-165`). The one place the
isolation invariant is violated is the DB lock (A-2) — disk isolation does not help when the *query*
shares the receiver's lock.

---

## Priority-ordered fix list

| # | Finding | Severity | Files | Est. impact | Cost |
|---|---|---|---|---|---|
| 1 | 128-context fallback makes studies permanently undeliverable | **Critical** | `forwarder/handlers/dicom.py:105-112` | availability: affected studies never deliver | S |
| 2 | Disk-full purge loop spins forever on delete failure | **Critical** | `disk.py:115-123`, `spool/__init__.py:905-917` | 100% CPU + no recovery on failing disk | S |
| 3 | 1.3 MB test-only `axe.js` served, unauthenticated | **Critical** | `web/public/axe.js` | 6.1× payload for zero benefit | S |
| 4 | Two FULL-fsync commits per instance | **High** | `spool/__init__.py:568,355` | +45% receive throughput (~31→~45 inst/s) | S |
| 5 | One connection + one RLock serializes all threads | **High** | `spool/db.py:180-211` | 25 assoc → only 1.46× throughput; 718 ms acks | M |
| 6 | `/api/pipeline` materializes whole studies table per poll | **High** | `web/pipeline.py:142-145` | stalls receive path every 2 s | S |
| 7 | Duplicate C-MOVEs move the whole study per match | **High** | `reports/move.py:130-138` | ~2× study transferred per matched instance | S |
| 8 | Full-study datasets buffered in RAM | **High** | `reports/move.py:99-106` | O(study size) heap per retrieve | S |
| 9 | No transport timeouts (SFTP connect has none) | **High** | `sftp.py:103-115`, `dicom.py:73-77` | hung server pins 1 of 3 workers forever | S |
| 10 | `_requeue_complete_routes`: 1 txn per destination | **High** | `spool/__init__.py:422-440` | +D×8.5 ms on re-opened-study receive path | S |
| 11 | Pollers stack requests, no abort/visibility gate | **High** | `PipelineView.tsx:61-65`, `LogsView.tsx:27-31` | unbounded requests under slow backend | M |
| 12 | No route lazy loading / vendor chunk | **High** | `web/src/App.tsx:3-12`, `vite.config.ts` | unnecessary first-paint cost | S |
| 13 | `spool_num_bytes` full SUM scan in purge loop | **High** | `disk.py:138-150` | K+2 full scans per purge cycle | M |
| 14 | Perf gates don't cover the receive path | **Medium** | `scripts/check_perf_gates.py` | ceiling unmeasured; regressions pass CI | S |
| 15 | `count_routes_by_target` unbounded GROUP BY, uncached | **Medium** | `spool/db.py:674-688` | 300 k-row scan per 2 s poll | S |
| 16 | Audit export/verify unbounded, lock-holding | **Medium** | `web/routes.py:1044-1084` | multi-second full scan per HTTP GET | M |
| 17 | `complete`/`fail` non-atomic, 3 txns | **Medium** | `spool/__init__.py:697-752` | double `STUDY_SENT` under concurrency | S |
| 18 | Pipeline destination table unbounded + re-polled | **Medium** | `PipelineView.tsx:224-247` | up to N rows × 4 s | M |
| 19 | C-FIND enumerates whole study, no early stop | **Medium** | `reports/find.py:118-141` | O(instances) per report query | M |
| 20 | `AuthContext` unmemoized; ~200 lines fetch duplication | **Medium** | `AuthContext.tsx:95` + 9 pages | whole-tree re-render; blocks F-3 fixes | M |

*S = small (hours), M = medium (1-2 days).*

**If only four things get done:** (1) cap the SCU context list, (2) break the disk-purge loop on delete
failure, (3) merge the two receive-path commits into one transaction, (4) delete `web/public/axe.js`.
Items 1-3 fix correctness/availability defects on the two most important paths; item 4 removes a 6×
payload regression for free. Together, items 3 + 5 lift the measured receive ceiling from ~31 to
~45-52 inst/s on this class of storage — the difference between "US-01 passes on paper" and "US-01
passes under load."

---

## Methodology and reproducibility

- All measurements taken on `/dev/sda2` (ext4, 89% full) — **not** tmpfs. An initial run against `/tmp`
  reported a 0.02 ms fsync and was discarded as meaningless; the same test on ext4 reports 9.9 ms.
- Benchmarks are preserved in `.full-review/.perfbench/`:
  - `fsync.py` — raw `os.fsync` latency (file + directory) on ext4.
  - `store_bench.py` — production `Spool.store_instance` at 1 and 25 concurrent threads, real pydicom
    datasets, on-disk WAL database with `synchronous=FULL`.
  - `sqlite_bench.py` — 1-statement-per-transaction vs 2-statements-per-transaction commit cost.
- The 128-context ceiling (B-1) was reproduced by executing `forwarder/handlers/dicom.py:105-112`
  against the installed pynetdicom 3.0.4 and capturing the `ValueError`.
- Frontend and admin-API findings were produced by full source review of `web/src/**` and
  `src/mercure_gateway/web/**` plus `reports/**`; bundle sizes are from the built output in
  `src/mercure_gateway/web/static/assets/`.

**Caveat on absolute numbers:** this volume's ~10 ms fsync is slow (a typical NVMe SSD is 0.5-2 ms).
The *relative* conclusions — 1.46× scaling at 25 associations, 26% of receive time in the redundant
second commit, 718 ms median ack latency at the ceiling — are disk-independent, since every barrier
scales by the same factor.
