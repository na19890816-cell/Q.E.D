-- migration 078: frost_fragility_surfaces
-- Fragility Surface Index の計算結果テーブル
-- rerun-safe: DO$$ + IF NOT EXISTS

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = 'public'
      AND table_name = 'frost_fragility_surfaces'
  ) THEN
    CREATE TABLE frost_fragility_surfaces (
      surface_id                   UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
      candidate_id                 TEXT        NOT NULL,
      run_id                       TEXT        NOT NULL,
      trace_id                     TEXT        NOT NULL,
      -- FSI メトリクス
      fragility_surface_index      FLOAT       NOT NULL DEFAULT 0.0
                                   CHECK (fragility_surface_index BETWEEN 0.0 AND 1.0),
      local_stability_score        FLOAT       NOT NULL DEFAULT 1.0
                                   CHECK (local_stability_score BETWEEN 0.0 AND 1.0),
      fragility_penalty            FLOAT       NOT NULL DEFAULT 0.0,
      -- ベースライン
      baseline_sharpe              FLOAT       NOT NULL DEFAULT 0.0,
      baseline_rank_ic             FLOAT       NOT NULL DEFAULT 0.0,
      -- 統計
      mean_sharpe                  FLOAT       NOT NULL DEFAULT 0.0,
      std_sharpe                   FLOAT       NOT NULL DEFAULT 0.0,
      cv_sharpe                    FLOAT       NOT NULL DEFAULT 0.0,
      min_sharpe                   FLOAT       NOT NULL DEFAULT 0.0,
      max_sharpe                   FLOAT       NOT NULL DEFAULT 0.0,
      sharpe_degradation_ratio     FLOAT       NOT NULL DEFAULT 0.0,
      n_samples                    INT         NOT NULL DEFAULT 0,
      n_invalid                    INT         NOT NULL DEFAULT 0,
      -- Gate
      gate_pass                    BOOLEAN     NOT NULL DEFAULT TRUE,
      gate_reason                  TEXT,
      -- パラメータ別感度 JSONB
      breakdown_json               JSONB       NOT NULL DEFAULT '{}',
      -- メタ
      fsi_max_threshold            FLOAT       NOT NULL DEFAULT 0.40,
      dry_run                      BOOLEAN     NOT NULL DEFAULT FALSE,
      created_at                   TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at                   TIMESTAMPTZ NOT NULL DEFAULT now(),
      CONSTRAINT uq_fragility_cand_run UNIQUE (candidate_id, run_id)
    );

    CREATE INDEX idx_fragility_candidate  ON frost_fragility_surfaces (candidate_id);
    CREATE INDEX idx_fragility_run        ON frost_fragility_surfaces (run_id);
    CREATE INDEX idx_fragility_trace      ON frost_fragility_surfaces (trace_id);
    CREATE INDEX idx_fragility_fsi        ON frost_fragility_surfaces (fragility_surface_index DESC);
    CREATE INDEX idx_fragility_gate_fail  ON frost_fragility_surfaces (gate_pass)
      WHERE gate_pass = FALSE;

    COMMENT ON TABLE frost_fragility_surfaces IS
      'Fragility Surface Index: パラメータ摂動に対する局所安定性の曲面スコア。
       fragility_surface_index > FROST_FSI_MAX の場合は gate_pass=FALSE となり
       FROST v2 の fragility_penalty が増加する。';
  END IF;
END $$;

-- migration 078 完了
