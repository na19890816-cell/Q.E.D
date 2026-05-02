#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
ENV_FILE="${REPO_ROOT}/config/env/.env.local"
[[ -f "${ENV_FILE}" ]] && { set -a; source "${ENV_FILE}"; set +a; }
PG_DSN="${QED_PG_DSN:-postgresql://postgres:postgres@localhost:5432/qed_dev}"
echo "[migration] 017 + 018 ..."
psql "${PG_DSN}" -v ON_ERROR_STOP=1 -f "${REPO_ROOT}/qedschema/migrations/017_event_study_knowledge_artifact_bridge.sql"
psql "${PG_DSN}" -v ON_ERROR_STOP=1 -f "${REPO_ROOT}/qedschema/views/017_v_event_study_knowledge_artifacts.sql"
psql "${PG_DSN}" -v ON_ERROR_STOP=1 -f "${REPO_ROOT}/qedschema/migrations/018_event_study_artifact_links_bridge.sql"
echo "[OK] 017+018 applied"
