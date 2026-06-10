#!/usr/bin/env bash
# run_frost_engine.sh
# FROST Meta-Fitness Engine を実行する
#
# 使い方:
#   QED_PG_DSN=postgresql://... bash scripts/frost/run_frost_engine.sh
#   bash scripts/frost/run_frost_engine.sh --dry-run
#   bash scripts/frost/run_frost_engine.sh --batch-label my_batch_v1
#   bash scripts/frost/run_frost_engine.sh --top-k 10 --verbose
#
# 環境変数 (すべてオプション):
#   QED_PG_DSN          PostgreSQL DSN
#   FROST_BATCH_LABEL   バッチラベル
#   FROST_DRY_RUN       1=dry-run
#   FROST_TOP_K         選抜数 (デフォルト25)
#   FROST_VERBOSE       1=詳細ログ

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
PYTHONPATH="${PROJECT_ROOT}"
export PYTHONPATH

# デフォルト設定
PG_DSN="${QED_PG_DSN:-postgresql://postgres:postgres@localhost:5432/qed_dev}"
BATCH_LABEL="${FROST_BATCH_LABEL:-frost_v1}"
DRY_RUN="${FROST_DRY_RUN:-0}"
TOP_K="${FROST_TOP_K:-25}"
VERBOSE="${FROST_VERBOSE:-0}"

# コマンドライン引数パース
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        --batch-label)
            BATCH_LABEL="$2"
            shift 2
            ;;
        --top-k)
            TOP_K="$2"
            shift 2
            ;;
        --verbose)
            VERBOSE=1
            shift
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

echo "======================================================"
echo "  FROST Engine 実行"
echo "  batch_label : $BATCH_LABEL"
echo "  top_k       : $TOP_K"
echo "  dry_run     : $DRY_RUN"
echo "  verbose     : $VERBOSE"
echo "======================================================"

export QED_PG_DSN="$PG_DSN"
export FROST_BATCH_LABEL="$BATCH_LABEL"
export FROST_DRY_RUN="$DRY_RUN"
export FROST_TOP_K="$TOP_K"
export FROST_VERBOSE="$VERBOSE"
export FROST_ENABLED=1

python3 -m analytics.python.frost.frost_runner "$@" 2>&1

EXIT_CODE=$?
if [[ $EXIT_CODE -ne 0 ]]; then
    echo ""
    echo "[ERROR] FROST engine が異常終了しました (exit=$EXIT_CODE)"
    exit $EXIT_CODE
fi

echo ""
echo "======================================================"
echo "  FROST Engine 完了"
echo "======================================================"
