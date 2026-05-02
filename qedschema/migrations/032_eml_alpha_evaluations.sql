-- migration 032: eml_alpha_evaluations
-- fold ごとの評価指標テーブル
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema='public' AND table_name='eml_alpha_evaluations'
  ) THEN
    CREATE TABLE eml_alpha_evaluations (
      evaluation_id    TEXT        PRIMARY KEY,
      candidate_id     TEXT        NOT NULL REFERENCES eml_alpha_candidates(candidate_id),
      trace_id         TEXT        NOT NULL,
      fold_id          TEXT        NOT NULL,
      fold_start_at    DATE,
      fold_end_at      DATE,
      ic               NUMERIC,
      rank_ic          NUMERIC,
      ic_t_stat        NUMERIC,
      hit_rate         NUMERIC,
      r2_oos           NUMERIC,
      sharpe           NUMERIC,
      sortino          NUMERIC,
      calmar           NUMERIC,
      max_drawdown     NUMERIC,
      turnover         NUMERIC,
      cost_drag        NUMERIC,
      cost_adj_sharpe  NUMERIC,
      regime_tag       TEXT,
      score            NUMERIC     NOT NULL DEFAULT 0.0,
      metadata         JSONB       NOT NULL DEFAULT '{}',
      created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE(candidate_id, fold_id)
    );
    CREATE INDEX idx_eml_evals_candidate  ON eml_alpha_evaluations(candidate_id);
    CREATE INDEX idx_eml_evals_trace      ON eml_alpha_evaluations(trace_id);
    CREATE INDEX idx_eml_evals_fold       ON eml_alpha_evaluations(fold_id);
    RAISE NOTICE 'created eml_alpha_evaluations';
  ELSE
    RAISE NOTICE 'eml_alpha_evaluations already exists';
  END IF;
END $$;

INSERT INTO _migrations(filename) VALUES('032_eml_alpha_evaluations.sql')
  ON CONFLICT(filename) DO NOTHING;
SELECT '--- OK ---';
