-- view 062: v_frost_selection_summary
-- FROST 選抜サマリービュー
-- 実行ごと・決定ごとの集計と、review 待ちリストを提供
CREATE OR REPLACE VIEW v_frost_selection_summary AS
SELECT
    fr.run_id,
    fr.trace_id,
    fr.batch_label,
    fr.engine_version,
    fr.status        AS run_status,
    fr.dry_run,
    fr.started_at,
    fr.ended_at,

    -- 決定集計
    COUNT(sd.decision_id)                                          AS total_decisions,
    COUNT(*) FILTER (WHERE sd.decision = 'SELECTED')               AS selected_count,
    COUNT(*) FILTER (WHERE sd.decision = 'HOLD')                   AS hold_count,
    COUNT(*) FILTER (WHERE sd.decision = 'REJECTED')               AS rejected_count,
    COUNT(*) FILTER (WHERE sd.decision = 'REVIEW_REQUIRED')        AS review_required_count,

    -- 昇格適格
    COUNT(*) FILTER (WHERE sd.promotion_eligible = TRUE)           AS promotion_eligible_count,
    COUNT(*) FILTER (WHERE sd.review_status = 'pending'
                    AND sd.promotion_eligible = TRUE)               AS pending_review_count,
    COUNT(*) FILTER (WHERE sd.review_status = 'approved')          AS approved_count,

    -- 重複抑制
    COUNT(*) FILTER (WHERE sd.suppressed_by_dedup = TRUE)          AS dedup_suppressed_count,

    -- スコア集計 (SELECTED のみ)
    ROUND(AVG(fe.frost_score)
          FILTER (WHERE sd.decision = 'SELECTED'), 6)              AS avg_selected_frost_score,
    ROUND(MAX(fe.frost_score)
          FILTER (WHERE sd.decision = 'SELECTED'), 6)              AS max_selected_frost_score,
    ROUND(AVG(fe.oos_sharpe)
          FILTER (WHERE sd.decision = 'SELECTED'), 4)              AS avg_selected_oos_sharpe,
    ROUND(AVG(fe.pbo_score)
          FILTER (WHERE sd.decision = 'SELECTED'), 4)              AS avg_selected_pbo,

    -- Hard gate 集計
    COUNT(*) FILTER (WHERE fe.hard_gate_passed = FALSE)            AS hard_gate_fail_count,

    -- gate 失敗内訳 (REJECTED のみ上位 gate 名)
    ARRAY_REMOVE(
        ARRAY_AGG(DISTINCT
            CASE
                WHEN sd.decision = 'REJECTED'
                     AND jsonb_array_length(sd.gate_failures) > 0
                THEN sd.gate_failures ->> 0
                ELSE NULL
            END
        ),
        NULL
    )                                                               AS top_gate_failure_names

FROM frost_runs fr
LEFT JOIN frost_selection_decisions sd
    ON sd.run_id = fr.run_id
LEFT JOIN frost_evaluations fe
    ON fe.candidate_id = sd.candidate_id
    AND fe.run_id = sd.run_id
GROUP BY
    fr.run_id,
    fr.trace_id,
    fr.batch_label,
    fr.engine_version,
    fr.status,
    fr.dry_run,
    fr.started_at,
    fr.ended_at
ORDER BY fr.started_at DESC;

COMMENT ON VIEW v_frost_selection_summary IS
'実行ごとの選抜サマリー: 採択・保留・棄却・昇格適格・review 待ち件数を提供';
