-- =============================================================================
-- Migration 016: event_study → experiment_reports ブリッジテーブル
-- event_study_summary_runs → experiment_reports 昇格管理
-- =============================================================================

CREATE TABLE IF NOT EXISTS event_study_experiment_report_bridge (
    id                      UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id                  TEXT        NOT NULL REFERENCES event_study_summary_runs(run_id) ON DELETE CASCADE,
    experiment_run_id       UUID        REFERENCES experiment_runs(id),
    trace_id                TEXT        NOT NULL,
    report_title            TEXT        NOT NULL,
    report_summary          TEXT        NOT NULL DEFAULT '',
    report_markdown         TEXT        NOT NULL DEFAULT '',
    report_metadata         JSONB       NOT NULL DEFAULT '{}',
    -- report_metadata には candidate_code / hypothesis_code / artifact_tag 等を格納
    promotion_status        TEXT        NOT NULL DEFAULT 'pending',  -- pending|applied|dry_run|rejected|conflicted
    promoted_at             TIMESTAMPTZ,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT eserb_run_id_unique UNIQUE (run_id)
);

CREATE INDEX IF NOT EXISTS idx_eserb_trace_id           ON event_study_experiment_report_bridge (trace_id);
CREATE INDEX IF NOT EXISTS idx_eserb_experiment_run_id  ON event_study_experiment_report_bridge (experiment_run_id);
CREATE INDEX IF NOT EXISTS idx_eserb_promotion_status   ON event_study_experiment_report_bridge (promotion_status);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_eserb_updated_at') THEN
        CREATE TRIGGER trg_eserb_updated_at
            BEFORE UPDATE ON event_study_experiment_report_bridge
            FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
    END IF;
END;
$$;

INSERT INTO _migrations (filename, applied_at)
VALUES ('016_event_study_experiment_report_bridge', now())
ON CONFLICT (filename) DO NOTHING;
