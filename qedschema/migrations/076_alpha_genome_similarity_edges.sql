-- migration 076: alpha_genome_similarity_edges
-- Alpha Genome 類似度エッジテーブル（候補間の Genome コサイン類似度）
-- rerun-safe: DO$$ + IF NOT EXISTS

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = 'public'
      AND table_name = 'alpha_genome_similarity_edges'
  ) THEN
    CREATE TABLE alpha_genome_similarity_edges (
      edge_id             UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
      run_id              TEXT        NOT NULL,
      trace_id            TEXT        NOT NULL,
      candidate_id_a      TEXT        NOT NULL,
      candidate_id_b      TEXT        NOT NULL,
      cosine_similarity   FLOAT       NOT NULL DEFAULT 0.0
                          CHECK (cosine_similarity BETWEEN 0.0 AND 1.0),
      l2_distance         FLOAT       NOT NULL DEFAULT 0.0
                          CHECK (l2_distance >= 0.0),
      dominant_axis_match BOOLEAN     NOT NULL DEFAULT FALSE,
      is_near_duplicate   BOOLEAN     NOT NULL DEFAULT FALSE,
      threshold_used      FLOAT       NOT NULL DEFAULT 0.90,
      dry_run             BOOLEAN     NOT NULL DEFAULT FALSE,
      created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
      CONSTRAINT uq_genome_edge_run_pair UNIQUE (run_id, candidate_id_a, candidate_id_b),
      CONSTRAINT chk_genome_edge_pair_order
        CHECK (candidate_id_a < candidate_id_b)  -- 上三角行列のみ保存
    );

    CREATE INDEX idx_genome_edges_run         ON alpha_genome_similarity_edges (run_id);
    CREATE INDEX idx_genome_edges_trace       ON alpha_genome_similarity_edges (trace_id);
    CREATE INDEX idx_genome_edges_cand_a      ON alpha_genome_similarity_edges (candidate_id_a);
    CREATE INDEX idx_genome_edges_cand_b      ON alpha_genome_similarity_edges (candidate_id_b);
    CREATE INDEX idx_genome_edges_similarity  ON alpha_genome_similarity_edges (cosine_similarity DESC);
    CREATE INDEX idx_genome_edges_near_dup    ON alpha_genome_similarity_edges (is_near_duplicate)
      WHERE is_near_duplicate = TRUE;

    COMMENT ON TABLE alpha_genome_similarity_edges IS
      'Alpha Genome Layer: 候補間のゲノム類似度エッジ。
       candidate_id_a < candidate_id_b の制約で上三角のみ保存（対称性を活用）。
       is_near_duplicate=TRUE の行が FROST v2 での dedup 排除対象。';
  END IF;
END $$;

-- migration 076 完了
