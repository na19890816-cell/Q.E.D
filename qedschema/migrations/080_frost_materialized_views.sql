-- migration 080: frost_materialized_views
-- FROST 集計 View を Materialized View 化
-- 目的: ダッシュボード・verify・review の重い集計を高速化
-- rerun-safe: IF NOT EXISTS + CREATE MATERIALIZED VIEW IF NOT EXISTS

-- ---------------------------------------------------------------------------
-- 1. frost_candidate_summary_mv
--    候補ごとの最新評価・決定の集計ビュー
-- ---------------------------------------------------------------------------
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_matviews
    WHERE matviewname = 'frost_candidate_summary_mv'
  ) THEN
    CREATE MATERIALIZED VIEW frost_candidate_summary_mv AS
    SELECT
      fc.candidate_id,
      fc.batch_label,
      fc.run_date,
      fc.source_system,
      fc.trace_id,
      fe.run_id,
      fe.frost_score,
      fe.predictive_score,
      fe.oos_sharpe_score,
      fe.regime_stability_score,
      fe.selection_consistency_score,
      fe.capacity_score,
      fe.pbo_penalty,
      fe.turnover_penalty,
      fe.fragility_penalty,
      fe.complexity_penalty,
      fd.decision,
      fd.promotion_eligible,
      fd.review_required,
      fd.decided_at
    FROM frost_fitness_candidates fc
    LEFT JOIN frost_evaluations fe
      ON fc.candidate_id = fe.candidate_id
    LEFT JOIN frost_selection_decisions fd
      ON fc.candidate_id = fd.candidate_id
         AND fe.run_id    = fd.run_id
    WHERE fe.run_id IN (
      -- 最新 run_id のみ
      SELECT run_id FROM frost_runs
      WHERE status IN ('completed', 'dry_run')
      ORDER BY started_at DESC
      LIMIT 1
    );

    CREATE UNIQUE INDEX frost_candidate_summary_mv_pk
      ON frost_candidate_summary_mv (candidate_id, run_id);

    CREATE INDEX idx_frost_cand_summary_decision
      ON frost_candidate_summary_mv (decision, promotion_eligible);

    CREATE INDEX idx_frost_cand_summary_score
      ON frost_candidate_summary_mv (frost_score DESC NULLS LAST);
  END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 2. frost_run_stats_mv
--    バッチ実行ごとの集計統計ビュー
-- ---------------------------------------------------------------------------
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_matviews
    WHERE matviewname = 'frost_run_stats_mv'
  ) THEN
    CREATE MATERIALIZED VIEW frost_run_stats_mv AS
    SELECT
      fr.run_id,
      fr.batch_label,
      fr.engine_version,
      fr.started_at,
      fr.status,
      fr.dry_run,
      COUNT(fe.evaluation_id)                             AS evaluated_count,
      AVG(fe.frost_score)                                 AS avg_frost_score,
      PERCENTILE_CONT(0.5) WITHIN GROUP
        (ORDER BY fe.frost_score)                         AS median_frost_score,
      MAX(fe.frost_score)                                 AS max_frost_score,
      MIN(fe.frost_score)                                 AS min_frost_score,
      COUNT(*) FILTER (
        WHERE fd.decision = 'SELECTED'
      )                                                   AS selected_count,
      COUNT(*) FILTER (
        WHERE fd.decision = 'HOLD'
      )                                                   AS hold_count,
      COUNT(*) FILTER (
        WHERE fd.decision = 'REJECTED'
      )                                                   AS rejected_count,
      COUNT(*) FILTER (
        WHERE fd.decision = 'REVIEW_REQUIRED'
      )                                                   AS review_required_count,
      COUNT(*) FILTER (
        WHERE fd.promotion_eligible = TRUE
      )                                                   AS promotion_eligible_count
    FROM frost_runs fr
    LEFT JOIN frost_evaluations fe ON fr.run_id = fe.run_id
    LEFT JOIN frost_selection_decisions fd
      ON fr.run_id = fd.run_id
         AND fe.candidate_id = fd.candidate_id
    GROUP BY
      fr.run_id, fr.batch_label, fr.engine_version,
      fr.started_at, fr.status, fr.dry_run;

    CREATE UNIQUE INDEX frost_run_stats_mv_pk
      ON frost_run_stats_mv (run_id);

    CREATE INDEX idx_frost_run_stats_started
      ON frost_run_stats_mv (started_at DESC);
  END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 3. frost_decision_history_mv
--    決定履歴のロールアップ（候補の決定変遷を追跡）
-- ---------------------------------------------------------------------------
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_matviews
    WHERE matviewname = 'frost_decision_history_mv'
  ) THEN
    CREATE MATERIALIZED VIEW frost_decision_history_mv AS
    SELECT
      fd.candidate_id,
      fd.run_id,
      fd.decision,
      fd.promotion_eligible,
      fd.review_required,
      fd.frost_score_at_decision,
      fd.decided_at,
      fd.trace_id,
      ROW_NUMBER() OVER (
        PARTITION BY fd.candidate_id
        ORDER BY fd.decided_at DESC
      ) AS decision_recency_rank
    FROM frost_selection_decisions fd;

    CREATE INDEX idx_frost_dec_hist_candidate
      ON frost_decision_history_mv (candidate_id, decision_recency_rank);

    CREATE INDEX idx_frost_dec_hist_decision
      ON frost_decision_history_mv (decision, decided_at DESC);
  END IF;
END $$;

-- ---------------------------------------------------------------------------
-- Refresh 手順コメント
-- ---------------------------------------------------------------------------
-- 本番運用での Refresh:
--   REFRESH MATERIALIZED VIEW CONCURRENTLY frost_candidate_summary_mv;
--   REFRESH MATERIALIZED VIEW CONCURRENTLY frost_run_stats_mv;
--   REFRESH MATERIALIZED VIEW CONCURRENTLY frost_decision_history_mv;
--
-- 推奨タイミング:
--   - frost_runner.py の run 完了後に自動 REFRESH
--   - または cron で 1 時間ごとに REFRESH
--
-- 環境変数 FROST_MVIEW_ENABLED=1 の場合のみ frost_runner.py から呼び出す

-- migration 080 完了
