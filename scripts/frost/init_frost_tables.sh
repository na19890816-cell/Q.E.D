#!/usr/bin/env bash
# init_frost_tables.sh
# FROST テーブル・ビューを qed_dev (または指定 DB) に初期化する
#
# 使い方:
#   QED_PG_DSN=postgresql://... bash scripts/frost/init_frost_tables.sh
#   bash scripts/frost/init_frost_tables.sh --dry-run   # dry-run: SQL 表示のみ
#
# 前提:
#   - PostgreSQL に接続可能であること
#   - qedschema/migrations/060_frost_*.sql が存在すること
#   - qedschema/views/060_v_frost_*.sql が存在すること

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=1
fi

# PostgreSQL DSN
PG_DSN="${QED_PG_DSN:-postgresql://postgres:postgres@localhost:5432/qed_dev}"

echo "======================================================"
echo "  FROST テーブル初期化"
echo "  DSN : $PG_DSN"
echo "  DRY_RUN : $DRY_RUN"
echo "======================================================"

# Migration ファイル一覧
MIGRATIONS=(
    "060_frost_runs.sql"
    "061_frost_fitness_candidates.sql"
    "062_frost_evaluations.sql"
    "063_frost_selection_decisions.sql"
    "064_frost_promotion_bridges.sql"
    "065_frost_audit_event_bridges.sql"
)

# View ファイル一覧
VIEWS=(
    "060_v_frost_runs.sql"
    "061_v_frost_candidate_scores.sql"
    "062_v_frost_selection_summary.sql"
    "063_v_frost_promotion_status.sql"
)

echo ""
echo "--- [1/2] Migrations ---"
for f in "${MIGRATIONS[@]}"; do
    FPATH="${PROJECT_ROOT}/qedschema/migrations/${f}"
    if [[ ! -f "$FPATH" ]]; then
        echo "[SKIP] ${f} — ファイルが存在しません"
        continue
    fi
    echo "applying: ${f}"
    if [[ $DRY_RUN -eq 0 ]]; then
        psql "$PG_DSN" -f "$FPATH"
    else
        echo "  [DRY_RUN] psql $PG_DSN -f $FPATH"
    fi
done

echo ""
echo "--- [2/2] Views ---"
for f in "${VIEWS[@]}"; do
    FPATH="${PROJECT_ROOT}/qedschema/views/${f}"
    if [[ ! -f "$FPATH" ]]; then
        echo "[SKIP] ${f} — ファイルが存在しません"
        continue
    fi
    echo "applying: ${f}"
    if [[ $DRY_RUN -eq 0 ]]; then
        psql "$PG_DSN" -f "$FPATH"
    else
        echo "  [DRY_RUN] psql $PG_DSN -f $FPATH"
    fi
done

echo ""
echo "======================================================"
echo "  完了: FROST テーブル初期化"
echo "======================================================"
