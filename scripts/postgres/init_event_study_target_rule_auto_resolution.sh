#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
ENV_FILE="${REPO_ROOT}/config/env/.env.local"
[[ -f "${ENV_FILE}" ]] && { set -a; source "${ENV_FILE}"; set +a; }
PG_DSN="${QED_PG_DSN:-postgresql://postgres:postgres@localhost:5432/qed_dev}"
echo "[migration] 020 target_rule_auto_resolution ..."
psql "${PG_DSN}" -v ON_ERROR_STOP=1 -f "${REPO_ROOT}/qedschema/migrations/020_event_study_target_rule_auto_resolution.sql"
psql "${PG_DSN}" -v ON_ERROR_STOP=1 -f "${REPO_ROOT}/qedschema/views/020_v_event_study_target_rule_resolution_status.sql"
echo "[seed] 020 target rules ..."
psql "${PG_DSN}" -v ON_ERROR_STOP=1 -f "${REPO_ROOT}/qedschema/seeds/020_event_study_target_rule_seed.sql"
echo "[OK] 020 applied"
