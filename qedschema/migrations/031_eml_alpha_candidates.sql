-- migration 031: eml_alpha_candidates
-- EML 木の候補式テーブル
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema='public' AND table_name='eml_alpha_candidates'
  ) THEN
    CREATE TABLE eml_alpha_candidates (
      candidate_id         TEXT        PRIMARY KEY,
      run_id               TEXT        NOT NULL REFERENCES eml_alpha_runs(run_id),
      trace_id             TEXT        NOT NULL,
      tree_json            JSONB       NOT NULL DEFAULT '{}',
      tree_depth           INT         NOT NULL DEFAULT 2,
      node_count           INT         NOT NULL DEFAULT 1,
      compiled_safe_expr   TEXT,
      fitness_score        NUMERIC     NOT NULL DEFAULT 0.0,
      status               TEXT        NOT NULL DEFAULT 'candidate',
      rejection_reason     TEXT,
      run_metadata         JSONB       NOT NULL DEFAULT '{}',
      created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE INDEX idx_eml_alpha_candidates_run_id  ON eml_alpha_candidates(run_id);
    CREATE INDEX idx_eml_alpha_candidates_trace   ON eml_alpha_candidates(trace_id);
    CREATE INDEX idx_eml_alpha_candidates_status  ON eml_alpha_candidates(status);
    CREATE INDEX idx_eml_alpha_candidates_fitness ON eml_alpha_candidates(fitness_score DESC);
    RAISE NOTICE 'created eml_alpha_candidates';
  ELSE
    RAISE NOTICE 'eml_alpha_candidates already exists';
  END IF;
END $$;

INSERT INTO _migrations(filename) VALUES('031_eml_alpha_candidates.sql')
  ON CONFLICT(filename) DO NOTHING;
SELECT '--- OK ---';
