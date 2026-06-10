#!/usr/bin/env bash
# run_frost_promote.sh
# FROST で SELECTED かつ review 承認済みの候補を Q.E.D. に昇格する
#
# 使い方:
#   bash scripts/frost/run_frost_promote.sh
#   bash scripts/frost/run_frost_promote.sh --dry-run
#   bash scripts/frost/run_frost_promote.sh --run-id <UUID>
#
# 処理内容:
#   1. frost_promotion_bridges から pending レコードを取得
#   2. frost_selection_decisions.review_status='approved' のものを昇格
#   3. promotion_status='applied' に更新

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
PYTHONPATH="${PROJECT_ROOT}"
export PYTHONPATH

PG_DSN="${QED_PG_DSN:-postgresql://postgres:postgres@localhost:5432/qed_dev}"
DRY_RUN=0
RUN_ID=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        --run-id)
            RUN_ID="$2"
            shift 2
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

echo "======================================================"
echo "  FROST 昇格実行"
echo "  run_id  : ${RUN_ID:-'(all pending)'}"
echo "  dry_run : $DRY_RUN"
echo "======================================================"

export QED_PG_DSN="$PG_DSN"

python3 - <<PYEOF
import sys
sys.path.insert(0, '${PROJECT_ROOT}')
import psycopg
from analytics.python.io.postgres_frost_promotion_bridge import (
    get_pending_promotions, update_promotion_status
)

dsn = '${PG_DSN}'
run_id = '${RUN_ID}' or None
dry_run = ${DRY_RUN} == 1

with psycopg.connect(dsn) as conn:
    pending = get_pending_promotions(conn, run_id=run_id or None)
    print(f"[promote] pending レコード: {len(pending)} 件")

    promoted = 0
    skipped  = 0
    for rec in pending:
        cid    = rec['candidate_id']
        rid    = rec['run_id']

        # review_status 確認
        row = conn.execute(
            "SELECT review_status FROM frost_selection_decisions "
            "WHERE candidate_id=%s AND run_id=%s LIMIT 1",
            (cid, rid)
        ).fetchone()
        review_status = row[0] if row else 'pending'

        if review_status != 'approved':
            print(f"  [SKIP] {cid[:8]}... review_status={review_status}")
            skipped += 1
            continue

        if dry_run:
            print(f"  [DRY_RUN] promote {cid[:8]}... frost_score={rec.get('frost_score')}")
            promoted += 1
        else:
            update_promotion_status(conn, rid, cid, 'applied')
            print(f"  [APPLY] promoted {cid[:8]}... frost_score={rec.get('frost_score')}")
            promoted += 1

    print(f"[promote] 完了: promoted={promoted}, skipped={skipped}")
PYEOF

echo ""
echo "======================================================"
echo "  FROST 昇格 完了"
echo "======================================================"
