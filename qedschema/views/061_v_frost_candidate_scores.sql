-- view 061: v_frost_candidate_scores
-- 候補ごとの全スコア一覧ビュー
-- frost_fitness_candidates × frost_evaluations × frost_selection_decisions を結合
CREATE OR REPLACE VIEW v_frost_candidate_scores AS
SELECT
    fc.candidate_id,
    fc.run_id,
    fc.trace_id,
    fc.source_type,
    fc.source_candidate_id,
    fc.formula_text,
    fc.real_safe_formula_text,
    fc.complexity_score,
    fc.horizon,
    fc.candidate_hash,
    fc.status AS candidate_status,

    -- 評価スコア
    fe.evaluation_id,
    fe.predictive_score,
    fe.rank_ic,
    fe.ic,
    fe.ic_t_stat,
    fe.hit_rate,
    fe.oos_sharpe,
    fe.oos_sortino,
    fe.oos_calmar,
    fe.oos_max_drawdown,
    fe.regime_stability_score,
    fe.regime_pass_ratio,
    fe.crisis_sharpe,
    fe.bull_sharpe,
    fe.selection_consistency_score,
    fe.top_k_stability,
    fe.sign_stability,
    fe.capacity_score,
    fe.turnover,
    fe.avg_hold_days,
    fe.tail_risk_score,
    fe.var_5,
    fe.cvar_5,
    fe.downside_vol,
    fe.pbo_score,
    fe.turnover_penalty,
    fe.complexity_penalty,
    fe.drawdown_penalty,
    fe.fragility_penalty,
    fe.frost_score,
    fe.hard_gate_passed,
    fe.hard_gate_failures,

    -- 採択判断
    sd.decision_id,
    sd.decision,
    sd.decision_reason,
    sd.decision_rank,
    sd.promotion_eligible,
    sd.review_required,
    sd.review_status,
    sd.reviewed_at,
    sd.reviewed_by,
    sd.rejection_reasons,
    sd.gate_failures,
    sd.near_duplicate_of,
    sd.suppressed_by_dedup,

    -- 昇格状態
    pb.bridge_id,
    pb.promotion_status,
    pb.target_entity_type,
    pb.target_entity_id,
    pb.promoted_at,

    fc.created_at,
    fc.updated_at

FROM frost_fitness_candidates fc
LEFT JOIN frost_evaluations fe
    ON fe.candidate_id = fc.candidate_id
    AND fe.run_id = fc.run_id
LEFT JOIN frost_selection_decisions sd
    ON sd.candidate_id = fc.candidate_id
    AND sd.run_id = fc.run_id
LEFT JOIN frost_promotion_bridges pb
    ON pb.candidate_id = fc.candidate_id
    AND pb.run_id = fc.run_id
ORDER BY
    fc.run_id,
    fe.frost_score DESC NULLS LAST;

COMMENT ON VIEW v_frost_candidate_scores IS
'候補ごとのスコア全量ビュー: 評価・採択判断・昇格状態を一括参照可能';
