#!/usr/bin/env bash
# =============================================================================
# verify_event_study_bootstrap_master.sh
# 全テーブルの件数とサンプルを確認して検証する
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
ENV_FILE="${REPO_ROOT}/config/env/.env.local"
[[ -f "${ENV_FILE}" ]] && { set -a; source "${ENV_FILE}"; set +a; }

PG_DSN="${QED_PG_DSN:-postgresql://postgres:postgres@localhost:5432/qed_dev}"
TRACE_ID="${1:-}"

psql_q() { psql "${PG_DSN}" -t -c "$1"; }

echo "=== [verify] event_study_summary_runs ==="
psql_q "SELECT run_id, status, total_events, trace_id FROM event_study_summary_runs ORDER BY created_at DESC LIMIT 3;"

echo ""
echo "=== [verify] event_study_summaries (last run) ==="
psql_q "SELECT run_id, COUNT(*), AVG(car_from_t0)::numeric(10,4) as avg_car FROM event_study_summaries GROUP BY run_id ORDER BY 1 DESC LIMIT 3;"

echo ""
echo "=== [verify] event_study_experiment_report_bridge ==="
psql_q "SELECT run_id, promotion_status, report_title FROM event_study_experiment_report_bridge ORDER BY created_at DESC LIMIT 3;"

echo ""
echo "=== [verify] knowledge_artifacts ==="
psql_q "SELECT artifact_id, status, artifact_tag, title FROM knowledge_artifacts ORDER BY created_at DESC LIMIT 3;"

echo ""
echo "=== [verify] target_resolution_log ==="
psql_q "SELECT resolution_status, COUNT(*) FROM target_resolution_log GROUP BY resolution_status;"

echo ""
echo "=== [verify] artifact_links ==="
psql_q "SELECT target_type, link_status, COUNT(*) FROM artifact_links GROUP BY target_type, link_status;"

echo ""
echo "=== [verify] event_study_pipeline_audit ==="
psql_q "SELECT phase, decision, COUNT(*) FROM event_study_pipeline_audit GROUP BY phase, decision ORDER BY 1,2;"

if [[ -n "${TRACE_ID}" ]]; then
  echo ""
  echo "=== [verify] trace_id=${TRACE_ID} の監査ログ ==="
  psql_q "SELECT phase, event_type, decision, created_at FROM event_study_pipeline_audit WHERE trace_id='${TRACE_ID}' ORDER BY created_at;"
fi

echo ""
echo "[verify] 完了"
