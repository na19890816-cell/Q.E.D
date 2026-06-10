-- migration 064: frost_promotion_bridges
-- Q.E.D. 昇格接続テーブル
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema='public' AND table_name='frost_promotion_bridges'
  ) THEN
    CREATE TABLE frost_promotion_bridges (
      bridge_id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
      run_id                 UUID        NOT NULL REFERENCES frost_runs(run_id) ON DELETE CASCADE,
      candidate_id           UUID        NOT NULL REFERENCES frost_fitness_candidates(candidate_id) ON DELETE CASCADE,
      trace_id               TEXT        NOT NULL,

      target_entity_type     TEXT        NOT NULL DEFAULT 'candidate'
                             CHECK (target_entity_type IN ('candidate','hypothesis',
                                                            'knowledge_artifact','experiment_report')),
      target_entity_id       TEXT,
      artifact_id            TEXT,
      link_id                TEXT,

      promotion_status       TEXT        NOT NULL DEFAULT 'pending'
                             CHECK (promotion_status IN ('pending','applied','dry_run',
                                                          'rejected','conflicted','error')),
      promotion_payload_json JSONB       NOT NULL DEFAULT '{}',
      frost_score            NUMERIC(12,8),
      decision_rank          INT,

      promoted_at            TIMESTAMPTZ,
      error_message          TEXT,
      created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),

      CONSTRAINT uq_frost_promotion_bridges_run_candidate
        UNIQUE (run_id, candidate_id)
    );
    CREATE INDEX idx_frost_pb_run_id    ON frost_promotion_bridges(run_id);
    CREATE INDEX idx_frost_pb_candidate ON frost_promotion_bridges(candidate_id);
    CREATE INDEX idx_frost_pb_trace_id  ON frost_promotion_bridges(trace_id);
    CREATE INDEX idx_frost_pb_status    ON frost_promotion_bridges(promotion_status);
    RAISE NOTICE 'created frost_promotion_bridges';
  ELSE
    RAISE NOTICE 'frost_promotion_bridges already exists — skipping';
  END IF;
END $$;
