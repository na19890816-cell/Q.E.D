#!/usr/bin/env bash
# =============================================================================
# run_event_study_bootstrap_master.sh
# 全段を順番に実行する統合スクリプト
# Usage: bash scripts/postgres/run_event_study_bootstrap_master.sh [--dry-run]
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
ENV_FILE="${REPO_ROOT}/config/env/.env.local"

if [[ -f "${ENV_FILE}" ]]; then
  set -a; source "${ENV_FILE}"; set +a
fi

# --dry-run オプション
if [[ "${1:-}" == "--dry-run" ]]; then
  export EVENT_STUDY_WRITEBACK_DRY_RUN=true
  echo "[INFO] DRY_RUN モードで実行します"
fi

PG_DSN="${QED_PG_DSN:-postgresql://postgres:postgres@localhost:5432/qed_dev}"
PYTHON="${PYTHON:-python3}"
PYPATH="${REPO_ROOT}/analytics/python"

log() { echo "[$(date -u +%H:%M:%S)] $*"; }
die() { echo "[ERROR] $*" >&2; exit 1; }

# ===========================================================================
log "=== Step 0: DB 接続確認 ==="
psql "${PG_DSN}" -c "SELECT 1" > /dev/null || die "PostgreSQL 接続失敗: ${PG_DSN}"

# ===========================================================================
log "=== Step 1: Phase A — writeback ==="
TRACE_ID_FILE="/tmp/es_trace_id.txt"
cd "${REPO_ROOT}"
PYTHONPATH="${PYPATH}" "${PYTHON}" scripts/postgres/run_event_study_writeback.py \
  | tee /tmp/es_writeback.log

RUN_ID=$(grep "^RUN_ID=" /tmp/es_writeback.log | cut -d= -f2 || echo "")
TRACE_ID=$(grep "^TRACE_ID=" /tmp/es_writeback.log | cut -d= -f2 || echo "")
[[ -z "${RUN_ID}" ]] && die "writeback から RUN_ID を取得できませんでした"
echo "${TRACE_ID}" > "${TRACE_ID_FILE}"
log "run_id=${RUN_ID}  trace_id=${TRACE_ID}"

# ===========================================================================
log "=== Step 2: Phase B — experiment_report bridge ==="
PYTHONPATH="${PYPATH}" "${PYTHON}" - << PYEOF
import os, sys
sys.path.insert(0, '${PYPATH}')
from pg_io.postgres_conn import get_connection
from pg_io.postgres_audit_event_writer import AuditEventWriter
from pg_io.postgres_event_study_experiment_report_bridge import ExperimentReportBridge

run_id = '${RUN_ID}'
dry_run = os.environ.get('EVENT_STUDY_WRITEBACK_DRY_RUN','false').lower()=='true'

with get_connection() as conn:
    audit = AuditEventWriter(conn, strict=False)
    bridge = ExperimentReportBridge(conn, audit, dry_run=dry_run)
    result = bridge.promote(run_id)
    conn.commit()
    print(f"[experiment_report] {result}")
PYEOF

# ===========================================================================
log "=== Step 3: Phase C — knowledge_artifact bridge ==="
PYTHONPATH="${PYPATH}" "${PYTHON}" - > /tmp/es_ka.log 2>&1 << PYEOF
import os, sys
sys.path.insert(0, '${PYPATH}')
from pg_io.postgres_conn import get_connection
from pg_io.postgres_audit_event_writer import AuditEventWriter
from pg_io.postgres_event_study_knowledge_artifact_bridge import KnowledgeArtifactBridge

run_id = '${RUN_ID}'
dry_run = os.environ.get('EVENT_STUDY_WRITEBACK_DRY_RUN','false').lower()=='true'

with get_connection() as conn:
    audit = AuditEventWriter(conn, strict=False)
    bridge = KnowledgeArtifactBridge(conn, audit, dry_run=dry_run)
    result = bridge.promote(run_id)
    conn.commit()
    print(f"ARTIFACT_ID={result['artifact_id']}")
    print(f"[knowledge_artifact] {result}")
PYEOF
cat /tmp/es_ka.log

ARTIFACT_ID=$(grep "^ARTIFACT_ID=" /tmp/es_ka.log | cut -d= -f2 || echo "")
[[ -z "${ARTIFACT_ID}" ]] && die "knowledge_artifact から ARTIFACT_ID を取得できませんでした"
log "artifact_id=${ARTIFACT_ID}"

# ===========================================================================
log "=== Step 4: Phase D — target resolution ==="
PYTHONPATH="${PYPATH}" "${PYTHON}" - << PYEOF
import os, sys
sys.path.insert(0, '${PYPATH}')
from pg_io.postgres_conn import get_connection
from pg_io.postgres_audit_event_writer import AuditEventWriter
from pg_io.postgres_event_study_target_rule_resolver import TargetRuleResolver

artifact_id = '${ARTIFACT_ID}'
dry_run = os.environ.get('EVENT_STUDY_WRITEBACK_DRY_RUN','false').lower()=='true'

with get_connection() as conn:
    audit = AuditEventWriter(conn, strict=False)
    resolver = TargetRuleResolver(conn, audit, dry_run=dry_run)
    result = resolver.resolve(artifact_id)
    conn.commit()
    print(f"[target_resolution] status={result['resolution_status']} target={result.get('matched_target_id')}")
PYEOF

# ===========================================================================
log "=== Step 5: Phase E — artifact_links bridge ==="
PYTHONPATH="${PYPATH}" "${PYTHON}" - << PYEOF
import os, sys
sys.path.insert(0, '${PYPATH}')
from pg_io.postgres_conn import get_connection
from pg_io.postgres_audit_event_writer import AuditEventWriter
from pg_io.postgres_event_study_artifact_links_bridge import ArtifactLinksBridge

artifact_id = '${ARTIFACT_ID}'
dry_run = os.environ.get('EVENT_STUDY_WRITEBACK_DRY_RUN','false').lower()=='true'

with get_connection() as conn:
    audit = AuditEventWriter(conn, strict=False)
    bridge = ArtifactLinksBridge(conn, audit, dry_run=dry_run)
    result = bridge.create_links(artifact_id)
    conn.commit()
    print(f"[artifact_link] {result}")
PYEOF

# ===========================================================================
log "=== Step 6: Phase F — audit summary ==="
PYTHONPATH="${PYPATH}" "${PYTHON}" - << PYEOF
import sys, json
sys.path.insert(0, '${PYPATH}')
from pg_io.postgres_conn import get_connection
from pg_io.postgres_event_study_audit_bridge import fetch_audit_for_trace, fetch_resolution_summary

trace_id = '${TRACE_ID}'
with get_connection() as conn:
    audit_summary = fetch_audit_for_trace(conn, trace_id)
    resolution_summary = fetch_resolution_summary(conn, trace_id)

print("=== AUDIT SUMMARY ===")
print(json.dumps(audit_summary['summary'], indent=2, ensure_ascii=False))
print("=== RESOLUTION SUMMARY ===")
print(json.dumps(resolution_summary['counts'], indent=2, ensure_ascii=False))
PYEOF

log "=== bootstrap_master 完了 ==="
log "  run_id    : ${RUN_ID}"
log "  trace_id  : ${TRACE_ID}"
log "  artifact  : ${ARTIFACT_ID}"
