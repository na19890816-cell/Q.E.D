-- view 060: v_frost_runs
-- FROST 実行一覧ダッシュボード用ビュー
-- 最新の実行状態・進捗・summary を提供する
CREATE OR REPLACE VIEW v_frost_runs AS
SELECT
    fr.run_id,
    fr.trace_id,
    fr.batch_label,
    fr.source_batch_label,
    fr.engine_version,
    fr.status,
    fr.dry_run,

    -- 候補カウント
    fr.candidate_count,
    fr.evaluated_count,
    fr.selected_count,
    fr.hold_count,
    fr.rejected_count,
    fr.promotion_count,

    -- 進捗率
    CASE
        WHEN fr.candidate_count > 0
        THEN ROUND(fr.evaluated_count::NUMERIC / fr.candidate_count * 100, 1)
        ELSE 0
    END AS eval_progress_pct,

    CASE
        WHEN fr.evaluated_count > 0
        THEN ROUND(fr.selected_count::NUMERIC / fr.evaluated_count * 100, 1)
        ELSE 0
    END AS selection_rate_pct,

    CASE
        WHEN fr.evaluated_count > 0
        THEN ROUND(fr.rejected_count::NUMERIC / fr.evaluated_count * 100, 1)
        ELSE 0
    END AS rejection_rate_pct,

    -- スコア集計 (frost_evaluations から)
    stats.avg_frost_score,
    stats.max_frost_score,
    stats.min_frost_score,
    stats.avg_pbo_score,
    stats.hard_gate_pass_count,
    stats.hard_gate_fail_count,

    -- 実行時間
    fr.started_at,
    fr.ended_at,
    CASE
        WHEN fr.ended_at IS NOT NULL
        THEN EXTRACT(EPOCH FROM (fr.ended_at - fr.started_at))::INT
        ELSE EXTRACT(EPOCH FROM (now() - fr.started_at))::INT
    END AS elapsed_seconds,

    fr.error_message,
    fr.created_at,
    fr.updated_at

FROM frost_runs fr
LEFT JOIN (
    SELECT
        run_id,
        ROUND(AVG(frost_score)::NUMERIC, 6)   AS avg_frost_score,
        ROUND(MAX(frost_score)::NUMERIC, 6)   AS max_frost_score,
        ROUND(MIN(frost_score)::NUMERIC, 6)   AS min_frost_score,
        ROUND(AVG(pbo_score)::NUMERIC, 6)     AS avg_pbo_score,
        COUNT(*) FILTER (WHERE hard_gate_passed = TRUE)  AS hard_gate_pass_count,
        COUNT(*) FILTER (WHERE hard_gate_passed = FALSE) AS hard_gate_fail_count
    FROM frost_evaluations
    GROUP BY run_id
) stats ON stats.run_id = fr.run_id
ORDER BY fr.created_at DESC;

COMMENT ON VIEW v_frost_runs IS
'FROST 実行一覧ダッシュボード: 実行ごとの進捗・スコア集計・選抜率を提供';
