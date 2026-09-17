#!/usr/bin/env bash
# D2 — backup/restore drill (docs/guides/backup-restore.md).
#
# Proves, end to end, that the documented backup -> wipe -> restore procedure
# round-trips a node whose audit chain still verifies and whose undelivered
# work is still queued. Runs against a THROWAWAY instance: its own HOME (the
# head-anchor path is $HOME-keyed), spool, config, and ports. Never touches a
# live instance — see the "Isolation" notes below.
#
# Evidence lands in $DR/evidence/. Usage: bash scripts/d2_backup_restore_drill.sh
set -euo pipefail

REPO=${REPO:-/home/dev/Documents/mercurie/dicom-gateway}
DR=${DR:-/tmp/d2-drill}                       # throwaway root (backups + evidence live here)
PORT=${PORT:-8091}                            # drill web port (must be free)
DICOM_PORT=${DICOM_PORT:-11123}               # drill receiver port
SEED=${SEED:-$REPO/.spool}                    # fixture studies to send (read-only)

H=$DR/home
PL=$H/.local/share/mercure-gateway            # platform data dir (anchors live here)
SP=$PL/spool
GW=$REPO/.venv/bin/mercure-gateway

log() { printf '\n=== %s ===\n' "$*"; }

# ---------------------------------------------------------------- 0. scaffold
log "scaffold (throwaway HOME=$H)"
rm -rf "$DR"; mkdir -p "$PL" "$H/.config/mercure-gateway" "$DR/evidence" "$DR/seed"
cat > "$DR/gateway.json" <<EOF
{
  "config_version": "1",
  "general": { "appliance_name": "D2-DRILL" },
  "receiver": { "ae_title": "DRILL", "port": $DICOM_PORT, "auto_enqueue_delay_sec": 2.0 },
  "storage": { "spool_dir": "$SP" },
  "web_ui": { "host": "127.0.0.1", "port": $PORT, "auth_enabled": false },
  "destinations": [
    {"name": "unreachable-pacs", "type": "dicom", "host": "127.0.0.1", "port": 42999, "aet_target": "NOWHERE"}
  ]
}
EOF
# The destination is deliberately unreachable: the studies must stay
# undelivered so criterion (d) has pending work to prove survived.
STUDIES=$(ls -d "$SEED"/*/ | head -2)
i=0; for s in $STUDIES; do i=$((i+1)); cp -a "$s" "$DR/seed/study$(printf %c $((96+i)))"; done
echo "seeded $(find "$DR/seed" -name '*.dcm' | wc -l) instances across $(ls -d "$DR"/seed/* | wc -l) studies"

start() { HOME=$H nohup "$GW" --config "$DR/gateway.json" --web --port "$PORT" >> "$DR/gw.log" 2>&1 &
          for _ in $(seq 1 60); do  # wait for the web port, not a fixed delay
            curl -fsS --max-time 2 "http://127.0.0.1:$PORT/api/system/health" >/dev/null 2>&1 && return 0
            sleep 1
          done
          echo "FATAL: drill gateway did not become healthy on :$PORT" >&2; return 1; }
stop()  { local p; p=$(pgrep -f "mercure-gateway --config $DR/gateway.json" | head -1 || true)
          [ -n "$p" ] && { kill -TERM "$p"; for _ in $(seq 1 40); do kill -0 "$p" 2>/dev/null || break; sleep 0.5; done; }; }
wipe()  { stop; rm -rf "$SP" "$PL/audit-heads.txt" "$PL/audit-heads-signed.jsonl"; }

# ---------------------------------------------------------------- 1. load data
log "start drill instance + send studies"
start
for d in "$DR"/seed/*/; do
  # shellcheck disable=SC2046
  storescu -aet MODALITY -aec DRILL 127.0.0.1 "$DICOM_PORT" $(find "$d" -name '*.dcm' | sort) >/dev/null 2>&1 || true
done
sleep 10

log "pre-backup state"
{
  echo "health: $(curl -fsS http://127.0.0.1:$PORT/api/system/health)"
  echo "audit:  $(curl -fsS http://127.0.0.1:$PORT/api/audit/verify)"
  echo "queue:  $(curl -fsS http://127.0.0.1:$PORT/api/queue/stats)"
  echo "studies on disk: $(ls "$SP" | grep -vE 'mercure|operations' | wc -l)"
  echo "dcm files:       $(find "$SP" -name '*.dcm' | wc -l)"
  echo "anchor lines:    $(wc -l < "$PL/audit-heads.txt")"
} | tee "$DR/evidence/01-pre-backup.txt"

# ---------------------------------------------------------------- 2. backups
log "Option A backup (stop, copy tree incl -wal, restart)"
stop
BK_A=$DR/BK-A; mkdir -p "$BK_A"
cp -a "$SP/." "$BK_A/spool/"
cp -a "$PL/audit-heads.txt" "$BK_A/" 2>/dev/null || echo "  WARN: no anchor file"
cp -a "$PL/audit-heads-signed.jsonl" "$BK_A/" 2>/dev/null || echo "  (no signed anchors — hub reporting off; C2 pending)"
cp -a "$DR/gateway.json" "$BK_A/mercure-gateway.json"; chmod 700 "$BK_A"
start

log "Option B backup (online, consistent snapshot; sqlite3 CLI absent -> VACUUM INTO)"
BK_B=$DR/BK-B; mkdir -p "$BK_B"
python3 - "$SP" "$BK_B" <<'PY'
import sqlite3, sys, os
src, bk = sys.argv[1], sys.argv[2]
dst = f"{bk}/mercure-gateway.db"
if os.path.exists(dst): os.remove(dst)
c = sqlite3.connect(f"file:{src}/mercure-gateway.db?mode=ro", uri=True)
c.execute(f"VACUUM INTO '{dst}'"); c.close()
v = sqlite3.connect(f"file:{dst}?mode=ro", uri=True)
print("  integrity_check:", v.execute("PRAGMA integrity_check").fetchone()[0])
PY
cp -a "$SP" "$BK_B/spool-files"                     # best-effort DICOM copy
rm -f "$BK_B/spool-files/mercure-gateway.db"*       # snapshot is self-contained; no sidecars
cp -a "$DR/gateway.json" "$BK_B/mercure-gateway.json"
cp -a "$PL/audit-heads.txt" "$BK_B/" 2>/dev/null

# ---------------------------------------------------------------- 3. disaster
log "TOTAL LOSS: wipe spool + anchors + config"
wipe
rm -f "$DR/gateway.json"
echo "  spool present: $([ -d "$SP" ] && echo yes || echo NO); anchors: $([ -f "$PL/audit-heads.txt" ] && echo yes || echo NO)"

# ---------------------------------------------------------------- 4. restore A
log "restore from BK-A (runbook steps 1-4)"
export MERCURE_GATEWAY_SPOOL="$SP" MERCURE_GATEWAY_CONFIG="$DR/gateway.json"
mkdir -p "$SP" "$H/.config/mercure-gateway"
cp -a "$BK_A/mercure-gateway.db" "$SP/" 2>/dev/null || cp -a "$BK_A/spool/mercure-gateway.db" "$SP/"
cp -a "$BK_A/spool/." "$SP/"
cp -a "$BK_A/audit-heads.txt" "$PL/" 2>/dev/null || echo "  WARN: no anchor file in BK-A"
cp -a "$BK_A/audit-heads-signed.jsonl" "$PL/" 2>/dev/null || echo "  (no signed anchors — C2 pending)"
cp -a "$BK_A/mercure-gateway.json" "$MERCURE_GATEWAY_CONFIG"
start

log "verify after BK-A restore -> evidence/02-post-restore-A.txt"
verify() { { # $1 = label, $2 = expect-studies, $3 = expect-dcm
  echo "a) health: $(curl -fsS http://127.0.0.1:$PORT/api/system/health)"
  echo "b) audit:  $(curl -fsS http://127.0.0.1:$PORT/api/audit/verify)"
  echo "d) queue:  $(curl -fsS http://127.0.0.1:$PORT/api/queue/stats)"
  echo "   studies on disk: $(ls "$SP" | grep -vE 'mercure|operations' | wc -l)  [expect $2]"
  echo "   dcm files:       $(find "$SP" -name '*.dcm' | wc -l)  [expect $3]"
  echo "   anchor lines:    $(wc -l < "$PL/audit-heads.txt" 2>/dev/null || echo 0)"
} | tee "$DR/evidence/$1"; }
verify 02-post-restore-A.txt 2 "$(find "$DR"/seed -name '*.dcm' | wc -l)"

# The anchor coverage map must run here: after the BK-A restore the anchor
# file still holds its full history, so every anchored head can be mapped
# back to an event. Control 2 below wipes that history on purpose.
log "anchor coverage map (which event types reach the external anchor file)"
python3 - "$SP" "$PL" <<'PY' | tee "$DR/evidence/05-anchor-coverage.txt"
import sqlite3, sys
from collections import Counter
sp, pl = sys.argv[1], sys.argv[2]
heads = {l.strip() for l in open(f"{pl}/audit-heads.txt") if l.strip()}
c = sqlite3.connect(f"file:{sp}/mercure-gateway.db?mode=ro", uri=True); c.row_factory = sqlite3.Row
rows = list(c.execute("SELECT id,event,hash FROM audit_events ORDER BY id"))
tot, anc = Counter(), Counter()
for r in rows:
    tot[r["event"]] += 1
    if r["hash"] in heads: anc[r["event"]] += 1
print(f"events={len(rows)}  distinct anchored heads present in chain={len(heads & {r['hash'] for r in rows})}")
for k in sorted(tot):
    mark = "" if anc[k] == tot[k] else "   <-- GAP"
    print(f"  {k:<16} {anc[k]}/{tot[k]}{mark}")
PY

# ---------------------------------------------------------------- 5. restore B
log "restore from BK-B (online snapshot path)"
wipe; rm -f "$DR/gateway.json"
mkdir -p "$SP"
cp -a "$BK_B/mercure-gateway.db" "$SP/"
cp -a "$BK_B/spool-files/." "$SP/"
cp -a "$BK_B/audit-heads.txt" "$PL/" 2>/dev/null || echo "  WARN: no anchor file in BK-B"
cp -a "$BK_B/mercure-gateway.json" "$DR/gateway.json"
start
verify 03-post-restore-B.txt 2 "$(find "$DR"/seed -name '*.dcm' | wc -l)"

# ---------------------------------------------------------------- 6. controls
log "CONTROL 2: restore WITHOUT anchors must be SILENTLY accepted (why (c) is the only sentinel)"
wipe; rm -f "$DR/gateway.json"
mkdir -p "$SP"
cp -a "$BK_B/mercure-gateway.db" "$SP/"; cp -a "$BK_B/spool-files/." "$SP/"
cp -a "$BK_B/mercure-gateway.json" "$DR/gateway.json"
# NOTE: audit-heads.txt deliberately NOT restored
start
{ echo "health (no anchors): $(curl -fsS http://127.0.0.1:$PORT/api/system/health)"
  echo "audit  (no anchors): $(curl -fsS http://127.0.0.1:$PORT/api/audit/verify)  <- chain verifies anyway"
  echo "queue  (no anchors): $(curl -fsS http://127.0.0.1:$PORT/api/queue/stats)"
  echo "log lines mentioning a missing anchor: $(grep -ci anchor "$DR/gw.log" || true)"
} | tee "$DR/evidence/04-control2-no-anchors.txt"

stop
log "drill complete — evidence in $DR/evidence/"
