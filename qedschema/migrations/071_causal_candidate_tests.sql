-- migration 071: causal_candidate_tests
-- 候補別 Causal Discovery テスト結果テーブル
-- rerun-safe: DO$$ + IF NOT EXISTS

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = 'public'
      AND table_name = 'causal_candidate_tests'
  ) THEN
    CREATE TABLE causal_candidate_tests (
      test_id                         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
      candidate_id                    TEXT        NOT NULL,
      run_id                          TEXT        NOT NULL,
      trace_id                        TEXT        NOT NULL,
      -- 因果方向性
      causal_direction_score          FLOAT       NOT NULL DEFAULT 0.0
                                      CHECK (causal_direction_score BETWEEN 0.0 AND 1.0),
      forward_correlation             FLOAT       NOT NULL DEFAULT 0.0,
      backward_correlation            FLOAT       NOT NULL DEFAULT 0.0,
      direction_asymmetry             FLOAT       NOT NULL DEFAULT 0.0,
      granger_proxy_score             FLOAT       NOT NULL DEFAULT 0.0,
      -- 不変性
      invariance_pass_ratio           FLOAT       NOT NULL DEFAULT 0.0
                                      CHECK (invariance_pass_ratio BETWEEN 0.0 AND 1.0),
      coefficient_stability           FLOAT       NOT NULL DEFAULT 0.0,
      regime_consistency_score        FLOAT       NOT NULL DEFAULT 0.0,
      n_regimes_tested                INT         NOT NULL DEFAULT 0,
      n_regimes_passed                INT         NOT NULL DEFAULT 0,
      -- 総合診断
      intervention_consistency_score  FLOAT       NOT NULL DEFAULT 0.0,
      confounding_risk_score          FLOAT       NOT NULL DEFAULT 0.0,
      causal_composite_score          FLOAT       NOT NULL DEFAULT 0.0,
      -- Gate
      gate_pass                       BOOLEAN     NOT NULL DEFAULT TRUE,
      gate_reason                     TEXT,
      -- 詳細 JSONB
      direction_details_json          JSONB       NOT NULL DEFAULT '{}',
      invariance_details_json         JSONB       NOT NULL DEFAULT '{}',
      diagnostics_json                JSONB       NOT NULL DEFAULT '{}',
      -- メタ
      dry_run                         BOOLEAN     NOT NULL DEFAULT FALSE,
      created_at                      TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at                      TIMESTAMPTZ NOT NULL DEFAULT now(),
      CONSTRAINT uq_causal_test_cand_run UNIQUE (candidate_id, run_id)
    );

    CREATE INDEX idx_causal_test_candidate  ON causal_candidate_tests (candidate_id);
    CREATE INDEX idx_causal_test_run        ON causal_candidate_tests (run_id);
    CREATE INDEX idx_causal_test_trace      ON causal_candidate_tests (trace_id);
    CREATE INDEX idx_causal_test_gate_fail  ON causal_candidate_tests (gate_pass)
      WHERE gate_pass = FALSE;
    CREATE INDEX idx_causal_test_composite  ON causal_candidate_tests (causal_composite_score DESC);

    COMMENT ON TABLE causal_candidate_tests IS
      'Causal Discovery Layer: 候補別の因果検定結果。
       gate_pass=FALSE の候補は FROST v2 で causal_validity_score が低下する。';
  END IF;
END $$;

-- migration 071 完了
