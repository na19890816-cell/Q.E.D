-- migration 070: causal_runs
-- Causal Discovery Layer の実行単位テーブル
-- rerun-safe: DO$$ + IF NOT EXISTS

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = 'public'
      AND table_name = 'causal_runs'
  ) THEN
    CREATE TABLE causal_runs (
      run_id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
      frost_run_id    TEXT,
      trace_id        TEXT        NOT NULL,
      batch_label     TEXT        NOT NULL DEFAULT 'causal_v1',
      candidate_count INT         NOT NULL DEFAULT 0,
      pass_count      INT         NOT NULL DEFAULT 0,
      fail_count      INT         NOT NULL DEFAULT 0,
      pass_ratio      FLOAT       NOT NULL DEFAULT 0.0,
      lag_used        INT         NOT NULL DEFAULT 1,
      n_regimes       INT         NOT NULL DEFAULT 4,
      status          TEXT        NOT NULL DEFAULT 'running'
                      CHECK (status IN ('running', 'completed', 'failed', 'skipped', 'dry_run')),
      dry_run         BOOLEAN     NOT NULL DEFAULT FALSE,
      error_message   TEXT,
      started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
      ended_at        TIMESTAMPTZ,
      created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE INDEX idx_causal_runs_trace    ON causal_runs (trace_id);
    CREATE INDEX idx_causal_runs_status   ON causal_runs (status, started_at DESC);
    CREATE INDEX idx_causal_runs_frost    ON causal_runs (frost_run_id)
      WHERE frost_run_id IS NOT NULL;

    COMMENT ON TABLE causal_runs IS
      'Causal Discovery Layer の実行単位。
       1 frost_run につき 1 causal_run が作成される。';
  END IF;
END $$;

-- migration 070 完了
