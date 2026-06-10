#!/usr/bin/env bash
# run_frost_backfill.sh
# 過去の EML 候補を FROST で遡及評価する
#
# 使い方:
#   bash scripts/frost/run_frost_backfill.sh --since 2024-01-01
#   bash scripts/frost/run_frost_backfill.sh --since 2024-01-01 --dry-run
#   bash scripts/frost/run_frost_backfill.sh --batch-label eml_backfill_v1
#
# 処理内容:
#   1. eml_alpha_candidates から since 以降の候補を取得
#   2. FROST で評価
#   3. frost_* テーブルに書き込み

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
PYTHONPATH="${PROJECT_ROOT}"
export PYTHONPATH

PG_DSN="${QED_PG_DSN:-postgresql://postgres:postgres@localhost:5432/qed_dev}"
BATCH_LABEL="frost_backfill_v1"
DRY_RUN=0
SINCE=""
LIMIT=500

while [[ $# -gt 0 ]]; do
    case "$1" in
        --since)
            SINCE="$2"
            shift 2
            ;;
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        --batch-label)
            BATCH_LABEL="$2"
            shift 2
            ;;
        --limit)
            LIMIT="$2"
            shift 2
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

echo "======================================================"
echo "  FROST Backfill"
echo "  since       : ${SINCE:-'(all)'}"
echo "  batch_label : $BATCH_LABEL"
echo "  limit       : $LIMIT"
echo "  dry_run     : $DRY_RUN"
echo "======================================================"

export QED_PG_DSN="$PG_DSN"
export FROST_BATCH_LABEL="$BATCH_LABEL"
export FROST_DRY_RUN="$DRY_RUN"
export FROST_ENABLED=1

python3 - <<PYEOF
import os, sys
sys.path.insert(0, '${PROJECT_ROOT}')
import psycopg
from analytics.python.frost.frost_config import load_frost_config
from analytics.python.frost.frost_runner import run_frost_pipeline, frost_candidates_from_eml

config = load_frost_config()
dsn = config.effective_pg_dsn()

with psycopg.connect(dsn) as conn:
    # EML 候補取得
    since_filter = "${SINCE}" if "${SINCE}" else None
    query = """
        SELECT candidate_id, run_id, trace_id,
               compiled_expr, fitness_score, metadata
        FROM eml_alpha_candidates
        WHERE status = 'promoted'
    """
    params = []
    if since_filter:
        query += " AND created_at >= %s"
        params.append(since_filter)
    query += " ORDER BY created_at DESC LIMIT ${LIMIT}"

    rows = conn.execute(query, params).fetchall()
    print(f"[backfill] {len(rows)} 件の EML 候補を取得")

    if not rows:
        print("[backfill] 候補なし — 終了")
        sys.exit(0)

    # FrostCandidate に変換 (簡易)
    from analytics.python.frost.frost_contracts import FrostCandidate
    import uuid, json
    candidates = []
    for row in rows:
        candidate_id, run_id, trace_id, expr, fitness, meta_raw = row
        meta = meta_raw if isinstance(meta_raw, dict) else (json.loads(meta_raw) if meta_raw else {})
        c = FrostCandidate(
            candidate_id=str(candidate_id),
            run_id=str(run_id) if run_id else str(uuid.uuid4()),
            trace_id=str(trace_id) if trace_id else str(uuid.uuid4()),
            source_type='eml',
            source_candidate_id=str(candidate_id),
            formula_text=str(expr or ''),
            complexity_score=0.5,
            backtest_summary=meta.get('backtest_summary', {'oos_sharpe': float(fitness or 0)}),
            metrics=meta.get('metrics', {}),
        )
        candidates.append(c)

    output = run_frost_pipeline(candidates, config, conn=conn)
    print(f"[backfill] 完了: selected={output.selected_count}, rejected={output.rejected_count}")
PYEOF

echo ""
echo "======================================================"
echo "  FROST Backfill 完了"
echo "======================================================"
