-- migration 060: frost_runs
-- FROST Meta-Fitness Engine の実行単位テーブル
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema='public' AND table_name='frost_runs'
  ) THEN
    CREATE TABLE frost_runs (
      run_id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
      trace_id             TEXT        NOT NULL,
      batch_label          TEXT        NOT NULL DEFAULT 'frost_v1',
      source_batch_label   TEXT,
      engine_version       TEXT        NOT NULL DEFAULT 'frost_v1',
      config_json          JSONB       NOT NULL DEFAULT '{}',
      candidate_count      INT         NOT NULL DEFAULT 0,
      evaluated_count      INT         NOT NULL DEFAULT 0,
      selected_count       INT         NOT NULL DEFAULT 0,
      hold_count           INT         NOT NULL DEFAULT 0,
      rejected_count       INT         NOT NULL DEFAULT 0,
      promotion_count      INT         NOT NULL DEFAULT 0,
      status               TEXT        NOT NULL DEFAULT 'running'
                           CHECK (status IN ('running','completed','failed','skipped','dry_run')),
      dry_run              BOOLEAN     NOT NULL DEFAULT FALSE,
      error_message        TEXT,
      started_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
      ended_at             TIMESTAMPTZ,
      created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE INDEX idx_frost_runs_trace_id   ON frost_runs(trace_id);
    CREATE INDEX idx_frost_runs_status     ON frost_runs(status);
    CREATE INDEX idx_frost_runs_batch      ON frost_runs(batch_label);
    CREATE INDEX idx_frost_runs_created    ON frost_runs(created_at DESC);
    RAISE NOTICE 'created frost_runs';
  ELSE
    RAISE NOTICE 'frost_runs already exists — skipping';
  END IF;
END $$;
