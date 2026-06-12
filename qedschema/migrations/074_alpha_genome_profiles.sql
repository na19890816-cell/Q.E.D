-- migration 074: alpha_genome_profiles
-- Alpha Genome Layer の候補別ゲノムプロファイルテーブル
-- rerun-safe: DO$$ + IF NOT EXISTS

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = 'public'
      AND table_name = 'alpha_genome_profiles'
  ) THEN
    CREATE TABLE alpha_genome_profiles (
      profile_id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
      candidate_id        TEXT        NOT NULL,
      run_id              TEXT        NOT NULL,
      trace_id            TEXT        NOT NULL,
      formula_text        TEXT        NOT NULL,
      -- Genome Vector (10軸)
      g_momentum          FLOAT       NOT NULL DEFAULT 0.0,
      g_mean_reversion    FLOAT       NOT NULL DEFAULT 0.0,
      g_flow              FLOAT       NOT NULL DEFAULT 0.0,
      g_volatility        FLOAT       NOT NULL DEFAULT 0.0,
      g_value             FLOAT       NOT NULL DEFAULT 0.0,
      g_event             FLOAT       NOT NULL DEFAULT 0.0,
      g_macro             FLOAT       NOT NULL DEFAULT 0.0,
      g_microstructure    FLOAT       NOT NULL DEFAULT 0.0,
      g_sentiment         FLOAT       NOT NULL DEFAULT 0.0,
      g_credit_leverage   FLOAT       NOT NULL DEFAULT 0.0,
      -- 集計指標
      dominant_axis       TEXT        NOT NULL DEFAULT 'momentum',
      novelty_score       FLOAT       NOT NULL DEFAULT 0.5
                          CHECK (novelty_score BETWEEN 0.0 AND 1.0),
      confidence          FLOAT       NOT NULL DEFAULT 0.0
                          CHECK (confidence BETWEEN 0.0 AND 1.0),
      -- 全ベクトルを JSONB でも保持（将来の軸追加・分析用）
      vector_json         JSONB       NOT NULL DEFAULT '{}',
      raw_scores_json     JSONB       NOT NULL DEFAULT '{}',
      -- メタ
      dry_run             BOOLEAN     NOT NULL DEFAULT FALSE,
      created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
      CONSTRAINT uq_genome_profile_cand_run UNIQUE (candidate_id, run_id)
    );

    -- インデックス
    CREATE INDEX idx_genome_profiles_candidate ON alpha_genome_profiles (candidate_id);
    CREATE INDEX idx_genome_profiles_run       ON alpha_genome_profiles (run_id);
    CREATE INDEX idx_genome_profiles_trace     ON alpha_genome_profiles (trace_id);
    CREATE INDEX idx_genome_profiles_dominant  ON alpha_genome_profiles (dominant_axis);
    CREATE INDEX idx_genome_profiles_novelty   ON alpha_genome_profiles (novelty_score DESC);
    CREATE INDEX idx_genome_profiles_vector_gin ON alpha_genome_profiles USING GIN (vector_json);

    COMMENT ON TABLE alpha_genome_profiles IS
      'Alpha Genome Layer: 候補式の因子DNAベクトルプロファイル。
       10軸 (momentum/mean_reversion/flow/volatility/value/event/macro/microstructure/sentiment/credit_leverage)
       でアルファ候補の本質的な因子露出を分解する。';
  END IF;
END $$;

-- migration 074 完了
