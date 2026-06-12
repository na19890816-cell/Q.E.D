-- migration 072: causal_invariance_results
-- レジーム別不変性テスト詳細結果テーブル
-- rerun-safe: DO$$ + IF NOT EXISTS

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = 'public'
      AND table_name = 'causal_invariance_results'
  ) THEN
    CREATE TABLE causal_invariance_results (
      result_id         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
      candidate_id      TEXT        NOT NULL,
      run_id            TEXT        NOT NULL,
      trace_id          TEXT        NOT NULL,
      regime_name       TEXT        NOT NULL,
      n_obs             INT         NOT NULL DEFAULT 0,
      beta              FLOAT       NOT NULL DEFAULT 0.0,
      alpha             FLOAT       NOT NULL DEFAULT 0.0,
      r_squared         FLOAT       NOT NULL DEFAULT 0.0,
      correlation       FLOAT       NOT NULL DEFAULT 0.0,
      is_positive       BOOLEAN     NOT NULL DEFAULT FALSE,
      is_significant    BOOLEAN     NOT NULL DEFAULT FALSE,
      dry_run           BOOLEAN     NOT NULL DEFAULT FALSE,
      created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
      CONSTRAINT uq_causal_inv_cand_run_regime UNIQUE (candidate_id, run_id, regime_name)
    );

    CREATE INDEX idx_causal_inv_candidate ON causal_invariance_results (candidate_id);
    CREATE INDEX idx_causal_inv_run       ON causal_invariance_results (run_id);
    CREATE INDEX idx_causal_inv_trace     ON causal_invariance_results (trace_id);
    CREATE INDEX idx_causal_inv_regime    ON causal_invariance_results (regime_name);

    COMMENT ON TABLE causal_invariance_results IS
      'Causal Discovery Layer: レジーム別不変性テストの詳細結果。
       各候補 × 各レジームの回帰結果を保存する。
       causal_candidate_tests.invariance_details_json の展開版。';
  END IF;
END $$;

-- migration 072 完了
