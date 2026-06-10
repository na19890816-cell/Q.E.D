-- migration 063: frost_selection_decisions
-- 採択判断テーブル (SELECTED / HOLD / REJECTED / REVIEW_REQUIRED)
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema='public' AND table_name='frost_selection_decisions'
  ) THEN
    CREATE TABLE frost_selection_decisions (
      decision_id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
      run_id                 UUID        NOT NULL REFERENCES frost_runs(run_id) ON DELETE CASCADE,
      candidate_id           UUID        NOT NULL REFERENCES frost_fitness_candidates(candidate_id) ON DELETE CASCADE,
      trace_id               TEXT        NOT NULL,

      decision               TEXT        NOT NULL
                             CHECK (decision IN ('SELECTED','HOLD','REJECTED','REVIEW_REQUIRED')),
      decision_reason        TEXT        NOT NULL DEFAULT '',
      decision_rank          INT,

      frost_score            NUMERIC(12,8) NOT NULL DEFAULT 0.0,
      promotion_eligible     BOOLEAN     NOT NULL DEFAULT FALSE,
      review_required        BOOLEAN     NOT NULL DEFAULT TRUE,
      review_status          TEXT        NOT NULL DEFAULT 'pending'
                             CHECK (review_status IN ('pending','approved','rejected','deferred')),
      reviewed_at            TIMESTAMPTZ,
      reviewed_by            TEXT,

      -- 棄却詳細
      rejection_reasons      JSONB       NOT NULL DEFAULT '[]',
      gate_failures          JSONB       NOT NULL DEFAULT '[]',

      -- 近似重複フラグ
      near_duplicate_of      UUID        REFERENCES frost_fitness_candidates(candidate_id),
      suppressed_by_dedup    BOOLEAN     NOT NULL DEFAULT FALSE,

      created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),

      CONSTRAINT uq_frost_selection_decisions_run_candidate
        UNIQUE (run_id, candidate_id)
    );
    CREATE INDEX idx_frost_sd_run_id    ON frost_selection_decisions(run_id);
    CREATE INDEX idx_frost_sd_candidate ON frost_selection_decisions(candidate_id);
    CREATE INDEX idx_frost_sd_trace_id  ON frost_selection_decisions(trace_id);
    CREATE INDEX idx_frost_sd_decision  ON frost_selection_decisions(decision);
    CREATE INDEX idx_frost_sd_eligible  ON frost_selection_decisions(promotion_eligible) WHERE promotion_eligible = TRUE;
    CREATE INDEX idx_frost_sd_rank      ON frost_selection_decisions(decision_rank) WHERE decision_rank IS NOT NULL;
    RAISE NOTICE 'created frost_selection_decisions';
  ELSE
    RAISE NOTICE 'frost_selection_decisions already exists — skipping';
  END IF;
END $$;
