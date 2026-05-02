-- migration 033: eml_alpha_promotion_bridge
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema='public' AND table_name='eml_alpha_promotion_bridge'
  ) THEN
    CREATE TABLE eml_alpha_promotion_bridge (
      bridge_id        TEXT        PRIMARY KEY,
      candidate_id     TEXT        NOT NULL REFERENCES eml_alpha_candidates(candidate_id),
      trace_id         TEXT        NOT NULL,
      report_id        TEXT,
      artifact_id      TEXT,
      link_id          TEXT,
      bridge_status    TEXT        NOT NULL DEFAULT 'pending',
      fitness_score    NUMERIC,
      promotion_reason TEXT,
      metadata         JSONB       NOT NULL DEFAULT '{}',
      created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE(candidate_id)
    );
    CREATE INDEX idx_eml_promo_bridge_candidate ON eml_alpha_promotion_bridge(candidate_id);
    CREATE INDEX idx_eml_promo_bridge_status    ON eml_alpha_promotion_bridge(bridge_status);
    RAISE NOTICE 'created eml_alpha_promotion_bridge';
  ELSE
    RAISE NOTICE 'eml_alpha_promotion_bridge already exists';
  END IF;
END $$;

INSERT INTO _migrations(filename) VALUES('033_eml_alpha_promotion_bridge.sql')
  ON CONFLICT(filename) DO NOTHING;
SELECT '--- OK ---';
