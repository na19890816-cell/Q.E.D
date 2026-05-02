-- migration 037: eml_backtest_trades
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema='public' AND table_name='eml_backtest_trades'
  ) THEN
    CREATE TABLE eml_backtest_trades (
      trade_id         TEXT        PRIMARY KEY,
      backtest_run_id  TEXT        NOT NULL REFERENCES eml_backtest_runs(backtest_run_id),
      fold_id          TEXT        NOT NULL,
      candidate_id     TEXT        NOT NULL,
      trace_id         TEXT        NOT NULL,
      symbol           TEXT        NOT NULL,
      entry_at         DATE        NOT NULL,
      entry_price      NUMERIC     NOT NULL DEFAULT 0.0,
      exit_at          DATE,
      exit_price       NUMERIC,
      size             NUMERIC     NOT NULL DEFAULT 0.0,
      side             TEXT        NOT NULL DEFAULT 'long',
      pnl              NUMERIC,
      pnl_pct          NUMERIC,
      cost             NUMERIC     NOT NULL DEFAULT 0.0,
      slippage         NUMERIC     NOT NULL DEFAULT 0.0,
      regime_tag       TEXT        NOT NULL DEFAULT 'normal',
      exit_reason      TEXT,
      metadata         JSONB       NOT NULL DEFAULT '{}',
      created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE INDEX idx_eml_trades_run       ON eml_backtest_trades(backtest_run_id);
    CREATE INDEX idx_eml_trades_fold      ON eml_backtest_trades(fold_id);
    CREATE INDEX idx_eml_trades_candidate ON eml_backtest_trades(candidate_id);
    CREATE INDEX idx_eml_trades_symbol    ON eml_backtest_trades(symbol);
    RAISE NOTICE 'created eml_backtest_trades';
  ELSE
    RAISE NOTICE 'eml_backtest_trades already exists';
  END IF;
END $$;

INSERT INTO _migrations(filename) VALUES('037_eml_backtest_trades.sql')
  ON CONFLICT(filename) DO NOTHING;
SELECT '--- OK ---';
