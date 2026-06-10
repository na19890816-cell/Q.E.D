#!/usr/bin/env bash
# verify_frost_engine.sh
# FROST エンジンの動作検証スクリプト (make frost-verify 相当)
#
# 使い方:
#   bash scripts/frost/verify_frost_engine.sh
#
# 確認項目:
#   1. テーブル・ビューの存在確認
#   2. 件数確認 (runs / candidates / evaluations / decisions)
#   3. PBO reject 件数
#   4. audit APPLIED/DRY_RUN/REJECTED 件数
#   5. promotion eligible 件数
#   6. near-duplicate 抑制件数
#   7. missing trace_id 件数 = 0 確認
#   8. null frost_score 件数 = 0 確認

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

PG_DSN="${QED_PG_DSN:-postgresql://postgres:postgres@localhost:5432/qed_dev}"

echo "======================================================"
echo "  FROST Engine 検証"
echo "  DSN: $PG_DSN"
echo "======================================================"

RUN_SQL() {
    psql "$PG_DSN" -t -A -c "$1" 2>/dev/null || echo "ERROR"
}

PRINT_COUNT() {
    local label="$1"
    local sql="$2"
    local val
    val=$(RUN_SQL "$sql")
    printf "  %-45s: %s\n" "$label" "$val"
}

echo ""
echo "--- テーブル存在確認 ---"
TABLES=(
    "frost_runs"
    "frost_fitness_candidates"
    "frost_evaluations"
    "frost_selection_decisions"
    "frost_promotion_bridges"
    "frost_audit_event_bridges"
)
VIEWS=(
    "v_frost_runs"
    "v_frost_candidate_scores"
    "v_frost_selection_summary"
    "v_frost_promotion_status"
)
ALL_OK=1
for t in "${TABLES[@]}"; do
    exists=$(RUN_SQL "SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name='${t}' LIMIT 1")
    if [[ "$exists" == "1" ]]; then
        printf "  [OK] %-40s\n" "$t"
    else
        printf "  [MISSING] %-40s\n" "$t"
        ALL_OK=0
    fi
done
for v in "${VIEWS[@]}"; do
    exists=$(RUN_SQL "SELECT 1 FROM information_schema.views WHERE table_schema='public' AND table_name='${v}' LIMIT 1")
    if [[ "$exists" == "1" ]]; then
        printf "  [OK] %-40s\n" "$v"
    else
        printf "  [MISSING] %-40s\n" "$v"
        ALL_OK=0
    fi
done
if [[ $ALL_OK -eq 0 ]]; then
    echo "  [WARNING] 不足しているオブジェクトがあります"
    echo "  → make frost-init を実行してください"
fi

echo ""
echo "--- 件数確認 ---"
PRINT_COUNT "frost_runs 件数"                    "SELECT COUNT(*) FROM frost_runs"
PRINT_COUNT "frost_fitness_candidates 件数"       "SELECT COUNT(*) FROM frost_fitness_candidates"
PRINT_COUNT "frost_evaluations 件数"              "SELECT COUNT(*) FROM frost_evaluations"
PRINT_COUNT "frost_selection_decisions 件数"      "SELECT COUNT(*) FROM frost_selection_decisions"
PRINT_COUNT "frost_promotion_bridges 件数"        "SELECT COUNT(*) FROM frost_promotion_bridges"
PRINT_COUNT "frost_audit_event_bridges 件数"      "SELECT COUNT(*) FROM frost_audit_event_bridges"

echo ""
echo "--- 決定内訳 ---"
PRINT_COUNT "SELECTED 件数"           "SELECT COUNT(*) FROM frost_selection_decisions WHERE decision='SELECTED'"
PRINT_COUNT "HOLD 件数"               "SELECT COUNT(*) FROM frost_selection_decisions WHERE decision='HOLD'"
PRINT_COUNT "REJECTED 件数"           "SELECT COUNT(*) FROM frost_selection_decisions WHERE decision='REJECTED'"
PRINT_COUNT "REVIEW_REQUIRED 件数"    "SELECT COUNT(*) FROM frost_selection_decisions WHERE decision='REVIEW_REQUIRED'"
PRINT_COUNT "promotion_eligible 件数" "SELECT COUNT(*) FROM frost_selection_decisions WHERE promotion_eligible=TRUE"
PRINT_COUNT "near-dup 抑制件数"       "SELECT COUNT(*) FROM frost_selection_decisions WHERE suppressed_by_dedup=TRUE"

echo ""
echo "--- スコア品質確認 ---"
PRINT_COUNT "PBO > 0.20 (hard gate reject)"         "SELECT COUNT(*) FROM frost_evaluations WHERE pbo_score > 0.20"
PRINT_COUNT "hard_gate FAIL 件数"                   "SELECT COUNT(*) FROM frost_evaluations WHERE hard_gate_passed=FALSE"
PRINT_COUNT "avg frost_score (SELECTED)"            "SELECT ROUND(AVG(e.frost_score)::numeric,4) FROM frost_evaluations e JOIN frost_selection_decisions d ON d.candidate_id=e.candidate_id WHERE d.decision='SELECTED'"
PRINT_COUNT "avg pbo_score (SELECTED)"              "SELECT ROUND(AVG(e.pbo_score)::numeric,4) FROM frost_evaluations e JOIN frost_selection_decisions d ON d.candidate_id=e.candidate_id WHERE d.decision='SELECTED'"

echo ""
echo "--- Audit Events 確認 ---"
PRINT_COUNT "audit APPLIED 件数"     "SELECT COUNT(*) FROM frost_audit_event_bridges WHERE decision='APPLIED'"
PRINT_COUNT "audit DRY_RUN 件数"     "SELECT COUNT(*) FROM frost_audit_event_bridges WHERE decision='DRY_RUN'"
PRINT_COUNT "audit REJECTED 件数"    "SELECT COUNT(*) FROM frost_audit_event_bridges WHERE decision='REJECTED'"
PRINT_COUNT "audit failed 件数"      "SELECT COUNT(*) FROM frost_audit_event_bridges WHERE event_status='failed'"

echo ""
echo "--- 昇格確認 ---"
PRINT_COUNT "promotion pending 件数"  "SELECT COUNT(*) FROM frost_promotion_bridges WHERE promotion_status='pending'"
PRINT_COUNT "promotion applied 件数"  "SELECT COUNT(*) FROM frost_promotion_bridges WHERE promotion_status='applied'"
PRINT_COUNT "promotion dry_run 件数"  "SELECT COUNT(*) FROM frost_promotion_bridges WHERE promotion_status='dry_run'"

echo ""
echo "--- データ品質チェック ---"
MISSING_TRACE=$(RUN_SQL "SELECT COUNT(*) FROM frost_runs WHERE trace_id IS NULL OR trace_id=''")
NULL_FROST=$(RUN_SQL "SELECT COUNT(*) FROM frost_evaluations WHERE frost_score IS NULL")
MISSING_TRACE_CAND=$(RUN_SQL "SELECT COUNT(*) FROM frost_fitness_candidates WHERE trace_id IS NULL OR trace_id=''")

printf "  %-45s: %s\n" "missing trace_id (frost_runs)"              "$MISSING_TRACE"
printf "  %-45s: %s\n" "missing trace_id (frost_fitness_candidates)" "$MISSING_TRACE_CAND"
printf "  %-45s: %s\n" "null frost_score (frost_evaluations)"        "$NULL_FROST"

# データ品質 FAIL 判定
QUALITY_OK=1
if [[ "$MISSING_TRACE" != "0" ]]; then
    echo "  [WARN] trace_id が欠損している frost_runs があります"
    QUALITY_OK=0
fi
if [[ "$NULL_FROST" != "0" ]]; then
    echo "  [WARN] frost_score が NULL の frost_evaluations があります"
    QUALITY_OK=0
fi

echo ""
if [[ $QUALITY_OK -eq 1 && $ALL_OK -eq 1 ]]; then
    echo "  [PASS] 全チェック通過"
else
    echo "  [WARN] 一部チェックで警告があります — 上記を確認してください"
fi
echo "======================================================"
