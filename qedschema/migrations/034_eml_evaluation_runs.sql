-- migration 034: eml_evaluation_runs
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema='public' AND table_name='eml_evaluation_runs'
  ) THEN
    CREATE TABLE eml_evaluation_runs (
      evaluation_run_id  TEXT        PRIMARY KEY,
      candidate_id       TEXT        NOT NULL REFERENCES eml_alpha_candidates(candidate_id),
      trace_id           TEXT        NOT NULL,
      run_kind           TEXT        NOT NULL DEFAULT 'full',
      fitness_kind       TEXT        NOT NULL DEFAULT 'rank_ic_cost_adj',
      total_folds        INT         NOT NULL DEFAULT 0,
      passed_folds       INT         NOT NULL DEFAULT 0,
      score              NUMERIC     NOT NULL DEFAULT 0.0,
      status             TEXT        NOT NULL DEFAULT 'pending',
      summary_metrics    JSONB       NOT NULL DEFAULT '{}',
      metadata           JSONB       NOT NULL DEFAULT '{}',
      created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE(candidate_id, run_kind)
    );
    CREATE INDEX idx_eml_eval_runs_candidate ON eml_evaluation_runs(candidate_id);
    CREATE INDEX idx_eml_eval_runs_trace     ON eml_evaluation_runs(trace_id);
    RAISE NOTICE 'created eml_evaluation_runs';
  ELSE
    RAISE NOTICE 'eml_evaluation_runs already exists';
  END IF;
END $$;

INSERT INTO _migrations(filename) VALUES('034_eml_evaluation_runs.sql')
  ON CONFLICT(filename) DO NOTHING;
SELECT '--- OK ---';
