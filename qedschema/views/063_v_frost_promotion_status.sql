-- view 063: v_frost_promotion_status
-- 昇格候補の状態追跡ビュー
-- review 承認 → Q.E.D. 昇格チェーンの状態を一覧する
CREATE OR REPLACE VIEW v_frost_promotion_status AS
SELECT
    pb.bridge_id,
    pb.run_id,
    pb.candidate_id,
    pb.trace_id,

    -- 候補情報
    fc.source_type,
    fc.source_candidate_id,
    fc.formula_text,
    fc.horizon,
    fc.complexity_score,
    fc.candidate_hash,

    -- スコア情報
    fe.frost_score,
    fe.oos_sharpe,
    fe.rank_ic,
    fe.pbo_score,
    fe.hard_gate_passed,

    -- 採択決定
    sd.decision,
    sd.decision_rank,
    sd.review_status,
    sd.reviewed_at,
    sd.reviewed_by,
    sd.promotion_eligible,
    sd.suppressed_by_dedup,

    -- 昇格状態
    pb.target_entity_type,
    pb.target_entity_id,
    pb.artifact_id,
    pb.promotion_status,
    pb.promotion_payload_json,
    pb.promoted_at,
    pb.error_message          AS promotion_error,

    -- audit bridge 状態 (最新イベント)
    latest_audit.event_name   AS last_audit_event,
    latest_audit.decision     AS last_audit_decision,
    latest_audit.event_status AS last_audit_status,
    latest_audit.occurred_at  AS last_audit_at,

    -- run 情報
    fr.batch_label,
    fr.engine_version,
    fr.dry_run,

    pb.created_at,
    pb.updated_at

FROM frost_promotion_bridges pb
JOIN frost_fitness_candidates fc
    ON fc.candidate_id = pb.candidate_id
JOIN frost_runs fr
    ON fr.run_id = pb.run_id
LEFT JOIN frost_evaluations fe
    ON fe.candidate_id = pb.candidate_id
    AND fe.run_id = pb.run_id
LEFT JOIN frost_selection_decisions sd
    ON sd.candidate_id = pb.candidate_id
    AND sd.run_id = pb.run_id
LEFT JOIN LATERAL (
    SELECT
        event_name,
        decision,
        event_status,
        occurred_at
    FROM frost_audit_event_bridges aeb
    WHERE aeb.candidate_id = pb.candidate_id
      AND aeb.run_id = pb.run_id
    ORDER BY occurred_at DESC
    LIMIT 1
) latest_audit ON TRUE
ORDER BY
    CASE pb.promotion_status
        WHEN 'pending' THEN 1
        WHEN 'applied' THEN 2
        WHEN 'dry_run' THEN 3
        WHEN 'error'   THEN 4
        ELSE 5
    END,
    fe.frost_score DESC NULLS LAST;

COMMENT ON VIEW v_frost_promotion_status IS
'昇格候補の状態追跡ビュー: review 承認から Q.E.D. 昇格チェーンまでの全状態を提供';
