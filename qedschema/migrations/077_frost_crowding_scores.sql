-- migration 077: frost_crowding_scores
-- Crowding Detector の候補別 crowding スコアテーブル
-- rerun-safe: DO$$ + IF NOT EXISTS

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = 'public'
      AND table_name = 'frost_crowding_scores'
  ) THEN
    CREATE TABLE frost_crowding_scores (
      score_id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
      candidate_id            TEXT        NOT NULL,
      run_id                  TEXT        NOT NULL,
      trace_id                TEXT        NOT NULL,
      -- Crowding メトリクス
      crowding_r2             FLOAT       NOT NULL DEFAULT 0.0
                              CHECK (crowding_r2 BETWEEN 0.0 AND 1.0),
      beta_concentration_score FLOAT      NOT NULL DEFAULT 0.0
                              CHECK (beta_concentration_score BETWEEN 0.0 AND 1.0),
      factor_overlap_score    FLOAT       NOT NULL DEFAULT 0.0
                              CHECK (factor_overlap_score BETWEEN 0.0 AND 1.0),
      crowding_penalty        FLOAT       NOT NULL DEFAULT 0.0,
      -- 最も類似した既知因子
      top_factor_id           TEXT,
      top_factor_r2           FLOAT       NOT NULL DEFAULT 0.0,
      -- Gate
      gate_pass               BOOLEAN     NOT NULL DEFAULT TRUE,
      gate_reason             TEXT,
      -- 全回帰結果 JSONB
      regressions_json        JSONB       NOT NULL DEFAULT '[]',
      -- メタ
      crowding_r2_threshold   FLOAT       NOT NULL DEFAULT 0.80,
      dry_run                 BOOLEAN     NOT NULL DEFAULT FALSE,
      created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
      CONSTRAINT uq_crowding_cand_run UNIQUE (candidate_id, run_id)
    );

    CREATE INDEX idx_crowding_candidate  ON frost_crowding_scores (candidate_id);
    CREATE INDEX idx_crowding_run        ON frost_crowding_scores (run_id);
    CREATE INDEX idx_crowding_trace      ON frost_crowding_scores (trace_id);
    CREATE INDEX idx_crowding_r2         ON frost_crowding_scores (crowding_r2 DESC);
    CREATE INDEX idx_crowding_gate_fail  ON frost_crowding_scores (gate_pass)
      WHERE gate_pass = FALSE;
    CREATE INDEX idx_crowding_top_factor ON frost_crowding_scores (top_factor_id)
      WHERE top_factor_id IS NOT NULL;

    COMMENT ON TABLE frost_crowding_scores IS
      'Crowding Detector: 候補シグナルの既知因子への露出スコア。
       crowding_r2 > FROST_CROWDING_R2_MAX の場合は gate_pass=FALSE となり
       FROST v2 の crowding_penalty が加算される。';
  END IF;
END $$;

-- migration 077 完了
