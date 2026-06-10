-- migration 065: frost_audit_event_bridges
-- audit_events 発行追跡テーブル
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema='public' AND table_name='frost_audit_event_bridges'
  ) THEN
    CREATE TABLE frost_audit_event_bridges (
      audit_bridge_id        UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
      run_id                 UUID        NOT NULL REFERENCES frost_runs(run_id) ON DELETE CASCADE,
      candidate_id           UUID        REFERENCES frost_fitness_candidates(candidate_id) ON DELETE SET NULL,
      trace_id               TEXT        NOT NULL,

      event_name             TEXT        NOT NULL,
      event_status           TEXT        NOT NULL DEFAULT 'emitted'
                             CHECK (event_status IN ('emitted','failed','skipped')),
      decision               TEXT        NOT NULL DEFAULT 'APPLIED'
                             CHECK (decision IN ('APPLIED','DRY_RUN','REJECTED','CONFLICTED')),
      audit_event_id         TEXT,
      payload_json           JSONB       NOT NULL DEFAULT '{}',

      occurred_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
      created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE INDEX idx_frost_aeb_run_id     ON frost_audit_event_bridges(run_id);
    CREATE INDEX idx_frost_aeb_candidate  ON frost_audit_event_bridges(candidate_id);
    CREATE INDEX idx_frost_aeb_trace_id   ON frost_audit_event_bridges(trace_id);
    CREATE INDEX idx_frost_aeb_event_name ON frost_audit_event_bridges(event_name);
    CREATE INDEX idx_frost_aeb_decision   ON frost_audit_event_bridges(decision);
    RAISE NOTICE 'created frost_audit_event_bridges';
  ELSE
    RAISE NOTICE 'frost_audit_event_bridges already exists — skipping';
  END IF;
END $$;
