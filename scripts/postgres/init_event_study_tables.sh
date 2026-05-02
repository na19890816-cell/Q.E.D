#!/usr/bin/env bash
# =============================================================================
# init_event_study_tables.sh
# Migration 015 を qed_dev に適用する
# Usage: bash scripts/postgres/init_event_study_tables.sh
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
ENV_FILE="${REPO_ROOT}/config/env/.env.local"

# .env.local 読み込み
if [[ -f "${ENV_FILE}" ]]; then
  set -a; source "${ENV_FILE}"; set +a
fi

PG_DSN="${QED_PG_DSN:-postgresql://postgres:postgres@localhost:5432/qed_dev}"

echo "[migration] 015_event_study_summary_tables ..."
psql "${PG_DSN}" -v ON_ERROR_STOP=1 \
  -f "${REPO_ROOT}/qedschema/migrations/015_event_study_summary_tables.sql"

echo "[view] 015_v_event_study_dashboard ..."
psql "${PG_DSN}" -v ON_ERROR_STOP=1 \
  -f "${REPO_ROOT}/qedschema/views/015_v_event_study_dashboard.sql"

echo "[OK] 015 applied"
