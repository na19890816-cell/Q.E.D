-- migration 061: frost_fitness_candidates
-- FROST に投入された候補カタログ
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema='public' AND table_name='frost_fitness_candidates'
  ) THEN
    CREATE TABLE frost_fitness_candidates (
      candidate_id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
      run_id                 UUID        NOT NULL REFERENCES frost_runs(run_id) ON DELETE CASCADE,
      trace_id               TEXT        NOT NULL,
      source_type            TEXT        NOT NULL DEFAULT 'eml'
                             CHECK (source_type IN ('eml','technical','event_derived',
                                                    'regime_aware','scrapling','existing_alpha')),
      source_candidate_id    TEXT,
      formula_text           TEXT,
      real_safe_formula_text TEXT,
      feature_spec_json      JSONB       NOT NULL DEFAULT '{}',
      complexity_score       NUMERIC(8,4) NOT NULL DEFAULT 0.0,
      horizon                TEXT        NOT NULL DEFAULT '5d',
      candidate_hash         TEXT        NOT NULL DEFAULT '',
      status                 TEXT        NOT NULL DEFAULT 'pending'
                             CHECK (status IN ('pending','evaluated','selected',
                                               'hold','rejected','review_required')),
      created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
      CONSTRAINT uq_frost_fitness_candidates_hash_run
        UNIQUE (run_id, candidate_hash)
    );
    CREATE INDEX idx_frost_fc_run_id    ON frost_fitness_candidates(run_id);
    CREATE INDEX idx_frost_fc_trace_id  ON frost_fitness_candidates(trace_id);
    CREATE INDEX idx_frost_fc_source    ON frost_fitness_candidates(source_type);
    CREATE INDEX idx_frost_fc_status    ON frost_fitness_candidates(status);
    CREATE INDEX idx_frost_fc_hash      ON frost_fitness_candidates(candidate_hash);
    RAISE NOTICE 'created frost_fitness_candidates';
  ELSE
    RAISE NOTICE 'frost_fitness_candidates already exists — skipping';
  END IF;
END $$;
