-- migration 030: eml_alpha_runs
-- EML alpha discovery の実行単位テーブル
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema='public' AND table_name='eml_alpha_runs'
  ) THEN
    CREATE TABLE eml_alpha_runs (
      run_id               TEXT        PRIMARY KEY,
      trace_id             TEXT        NOT NULL,
      batch_label          TEXT        NOT NULL,
      terminal_set_hash    TEXT        NOT NULL,
      target_horizon       TEXT        NOT NULL DEFAULT '5d',
      fitness_kind         TEXT        NOT NULL DEFAULT 'rank_ic_cost_adj',
      max_depth            INT         NOT NULL DEFAULT 3,
      max_nodes            INT         NOT NULL DEFAULT 8,
      status               TEXT        NOT NULL DEFAULT 'running',
      total_candidates     INT         NOT NULL DEFAULT 0,
      promoted_candidates  INT         NOT NULL DEFAULT 0,
      run_metadata         JSONB       NOT NULL DEFAULT '{}',
      started_at           TIMESTAMPTZ,
      completed_at         TIMESTAMPTZ,
      created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE INDEX idx_eml_alpha_runs_trace_id  ON eml_alpha_runs(trace_id);
    CREATE INDEX idx_eml_alpha_runs_status    ON eml_alpha_runs(status);
    CREATE INDEX idx_eml_alpha_runs_created   ON eml_alpha_runs(created_at DESC);
    RAISE NOTICE 'created eml_alpha_runs';
  ELSE
    RAISE NOTICE 'eml_alpha_runs already exists';
  END IF;
END $$;

INSERT INTO _migrations(filename) VALUES('030_eml_alpha_runs.sql')
  ON CONFLICT(filename) DO NOTHING;
SELECT '--- OK ---';
