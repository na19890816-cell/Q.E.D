#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
ENV_FILE="${REPO_ROOT}/config/env/.env.local"
[[ -f "${ENV_FILE}" ]] && { set -a; source "${ENV_FILE}"; set +a; }
PG_DSN="${QED_PG_DSN:-postgresql://postgres:postgres@localhost:5432/qed_dev}"
echo "[migration] 021 audit_events_integration ..."
psql "${PG_DSN}" -v ON_ERROR_STOP=1 -f "${REPO_ROOT}/qedschema/migrations/021_event_study_audit_events_integration.sql"
psql "${PG_DSN}" -v ON_ERROR_STOP=1 -f "${REPO_ROOT}/qedschema/views/021_v_event_study_audit_events.sql"
echo "[OK] 021 applied"
