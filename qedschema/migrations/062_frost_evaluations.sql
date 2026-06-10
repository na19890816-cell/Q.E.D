-- migration 062: frost_evaluations
-- 候補ごとの FROST 評価詳細
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema='public' AND table_name='frost_evaluations'
  ) THEN
    CREATE TABLE frost_evaluations (
      evaluation_id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
      run_id                     UUID        NOT NULL REFERENCES frost_runs(run_id) ON DELETE CASCADE,
      candidate_id               UUID        NOT NULL REFERENCES frost_fitness_candidates(candidate_id) ON DELETE CASCADE,
      trace_id                   TEXT        NOT NULL,

      -- 予測力
      predictive_score           NUMERIC(10,6) NOT NULL DEFAULT 0.0,
      rank_ic                    NUMERIC(10,6),
      ic                         NUMERIC(10,6),
      ic_t_stat                  NUMERIC(10,6),
      hit_rate                   NUMERIC(8,6),

      -- OOS 性能
      oos_sharpe                 NUMERIC(10,6),
      oos_sortino                NUMERIC(10,6),
      oos_calmar                 NUMERIC(10,6),
      oos_max_drawdown           NUMERIC(10,6),

      -- レジーム安定性
      regime_stability_score     NUMERIC(10,6),
      regime_pass_ratio          NUMERIC(8,6),
      crisis_sharpe              NUMERIC(10,6),
      bull_sharpe                NUMERIC(10,6),

      -- セレクション安定性
      selection_consistency_score NUMERIC(10,6),
      top_k_stability            NUMERIC(8,6),
      sign_stability             NUMERIC(8,6),

      -- キャパシティ・ターンオーバー
      capacity_score             NUMERIC(10,6),
      turnover                   NUMERIC(10,6),
      avg_hold_days              NUMERIC(10,4),

      -- リスク
      tail_risk_score            NUMERIC(10,6),
      var_5                      NUMERIC(10,6),
      cvar_5                     NUMERIC(10,6),
      downside_vol               NUMERIC(10,6),

      -- ペナルティ
      pbo_score                  NUMERIC(10,6) NOT NULL DEFAULT 0.0,
      turnover_penalty           NUMERIC(10,6) NOT NULL DEFAULT 0.0,
      complexity_penalty         NUMERIC(10,6) NOT NULL DEFAULT 0.0,
      drawdown_penalty           NUMERIC(10,6) NOT NULL DEFAULT 0.0,
      fragility_penalty          NUMERIC(10,6) NOT NULL DEFAULT 0.0,

      -- 総合スコア
      frost_score                NUMERIC(12,8) NOT NULL DEFAULT 0.0,

      -- 詳細 JSON
      metrics_json               JSONB NOT NULL DEFAULT '{}',
      backtest_json              JSONB NOT NULL DEFAULT '{}',
      regime_json                JSONB NOT NULL DEFAULT '{}',
      diagnostics_json           JSONB NOT NULL DEFAULT '{}',

      -- ガート判定
      hard_gate_passed           BOOLEAN NOT NULL DEFAULT TRUE,
      hard_gate_failures         JSONB   NOT NULL DEFAULT '[]',

      created_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),

      CONSTRAINT uq_frost_evaluations_run_candidate
        UNIQUE (run_id, candidate_id)
    );
    CREATE INDEX idx_frost_eval_run_id      ON frost_evaluations(run_id);
    CREATE INDEX idx_frost_eval_candidate   ON frost_evaluations(candidate_id);
    CREATE INDEX idx_frost_eval_trace_id    ON frost_evaluations(trace_id);
    CREATE INDEX idx_frost_eval_frost_score ON frost_evaluations(frost_score DESC);
    CREATE INDEX idx_frost_eval_pbo         ON frost_evaluations(pbo_score);
    RAISE NOTICE 'created frost_evaluations';
  ELSE
    RAISE NOTICE 'frost_evaluations already exists — skipping';
  END IF;
END $$;
