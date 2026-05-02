-- migration 036: eml_backtest_folds
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema='public' AND table_name='eml_backtest_folds'
  ) THEN
    CREATE TABLE eml_backtest_folds (
      fold_id              TEXT        PRIMARY KEY,
      backtest_run_id      TEXT        NOT NULL REFERENCES eml_backtest_runs(backtest_run_id),
      candidate_id         TEXT        NOT NULL,
      trace_id             TEXT        NOT NULL,
      fold_index           INT         NOT NULL,
      fold_start_at        DATE        NOT NULL,
      fold_end_at          DATE        NOT NULL,
      regime_tag           TEXT        NOT NULL DEFAULT 'normal',
      total_trades         INT         NOT NULL DEFAULT 0,
      sharpe               NUMERIC,
      sortino              NUMERIC,
      max_drawdown         NUMERIC,
      total_return         NUMERIC,
      turnover             NUMERIC,
      cost_drag            NUMERIC,
      win_rate             NUMERIC,
      metrics_json         JSONB       NOT NULL DEFAULT '{}',
      regime_breakdown_json JSONB      NOT NULL DEFAULT '{}',
      status               TEXT        NOT NULL DEFAULT 'pending',
      created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE(backtest_run_id, fold_index)
    );
    CREATE INDEX idx_eml_bt_folds_run       ON eml_backtest_folds(backtest_run_id);
    CREATE INDEX idx_eml_bt_folds_candidate ON eml_backtest_folds(candidate_id);
    CREATE INDEX idx_eml_bt_folds_regime    ON eml_backtest_folds(regime_tag);
    RAISE NOTICE 'created eml_backtest_folds';
  ELSE
    RAISE NOTICE 'eml_backtest_folds already exists';
  END IF;
END $$;

INSERT INTO _migrations(filename) VALUES('036_eml_backtest_folds.sql')
  ON CONFLICT(filename) DO NOTHING;
SELECT '--- OK ---';
