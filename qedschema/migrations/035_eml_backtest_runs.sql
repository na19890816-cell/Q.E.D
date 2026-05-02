-- migration 035: eml_backtest_runs
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema='public' AND table_name='eml_backtest_runs'
  ) THEN
    CREATE TABLE eml_backtest_runs (
      backtest_run_id    TEXT        PRIMARY KEY,
      candidate_id       TEXT        NOT NULL REFERENCES eml_alpha_candidates(candidate_id),
      trace_id           TEXT        NOT NULL,
      mode               TEXT        NOT NULL DEFAULT 'expanding',
      universe_tag       TEXT        NOT NULL DEFAULT 'us_largecap_v1',
      horizon_days       INT         NOT NULL DEFAULT 5,
      cost_bps           NUMERIC     NOT NULL DEFAULT 2.0,
      slippage_bps       NUMERIC     NOT NULL DEFAULT 2.0,
      gap_open_bps       NUMERIC     NOT NULL DEFAULT 3.0,
      regime_tag_set     TEXT[]      NOT NULL DEFAULT '{}',
      status             TEXT        NOT NULL DEFAULT 'pending',
      total_folds        INT         NOT NULL DEFAULT 0,
      summary_json       JSONB       NOT NULL DEFAULT '{}',
      started_at         TIMESTAMPTZ,
      finished_at        TIMESTAMPTZ,
      created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE INDEX idx_eml_bt_runs_candidate ON eml_backtest_runs(candidate_id);
    CREATE INDEX idx_eml_bt_runs_trace     ON eml_backtest_runs(trace_id);
    CREATE INDEX idx_eml_bt_runs_status    ON eml_backtest_runs(status);
    RAISE NOTICE 'created eml_backtest_runs';
  ELSE
    RAISE NOTICE 'eml_backtest_runs already exists';
  END IF;
END $$;

INSERT INTO _migrations(filename) VALUES('035_eml_backtest_runs.sql')
  ON CONFLICT(filename) DO NOTHING;
SELECT '--- OK ---';
