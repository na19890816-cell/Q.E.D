-- migration 075: alpha_genome_clusters
-- Alpha Genome クラスタリング結果テーブル
-- rerun-safe: DO$$ + IF NOT EXISTS

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = 'public'
      AND table_name = 'alpha_genome_clusters'
  ) THEN
    CREATE TABLE alpha_genome_clusters (
      cluster_record_id   UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
      run_id              TEXT        NOT NULL,
      trace_id            TEXT        NOT NULL,
      cluster_id          INT         NOT NULL,
      dominant_axis       TEXT        NOT NULL DEFAULT 'momentum',
      member_count        INT         NOT NULL DEFAULT 0,
      intra_cluster_sim   FLOAT       NOT NULL DEFAULT 0.0,
      centroid_json       JSONB       NOT NULL DEFAULT '{}',
      k_clusters          INT         NOT NULL DEFAULT 5,
      n_iterations        INT         NOT NULL DEFAULT 0,
      converged           BOOLEAN     NOT NULL DEFAULT FALSE,
      inertia             FLOAT       NOT NULL DEFAULT 0.0,
      member_ids_json     JSONB       NOT NULL DEFAULT '[]',
      dry_run             BOOLEAN     NOT NULL DEFAULT FALSE,
      created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
      CONSTRAINT uq_genome_cluster_run_cluster UNIQUE (run_id, cluster_id)
    );

    CREATE INDEX idx_genome_clusters_run     ON alpha_genome_clusters (run_id);
    CREATE INDEX idx_genome_clusters_trace   ON alpha_genome_clusters (trace_id);
    CREATE INDEX idx_genome_clusters_axis    ON alpha_genome_clusters (dominant_axis);

    COMMENT ON TABLE alpha_genome_clusters IS
      'Alpha Genome Layer: K-Means クラスタリング結果。
       1 run_id あたり k 件のクラスター行が格納される。';
  END IF;
END $$;

-- migration 075 完了
