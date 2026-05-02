-- =============================================================================
-- Migration 021: event_study_audit_events_integration
-- 各昇格フェーズの audit 補助テーブル (audit_events が使えない場合のフォールバック)
-- =============================================================================

-- event_study_pipeline_audit: パイプライン固有の監査補助テーブル
-- audit_events が使えない環境でも継続動作できる (non-strict mode)
CREATE TABLE IF NOT EXISTS event_study_pipeline_audit (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    trace_id        TEXT        NOT NULL,
    phase           TEXT        NOT NULL,       -- writeback|experiment_report|knowledge_artifact|target_resolution|artifact_link
    object_type     TEXT        NOT NULL,
    object_id       TEXT        NOT NULL,
    event_type      TEXT        NOT NULL,       -- TRANSITION_APPLIED|TRANSITION_DRY_RUN|TRANSITION_REJECTED|TRANSITION_CONFLICTED
    decision        TEXT        NOT NULL,       -- APPLIED|DRY_RUN|REJECTED|CONFLICTED
    decision_reason TEXT,
    metadata        JSONB       NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_espa_trace_id    ON event_study_pipeline_audit (trace_id);
CREATE INDEX IF NOT EXISTS idx_espa_phase       ON event_study_pipeline_audit (phase);
CREATE INDEX IF NOT EXISTS idx_espa_decision    ON event_study_pipeline_audit (decision);

INSERT INTO _migrations (filename, applied_at)
VALUES ('021_event_study_audit_events_integration', now())
ON CONFLICT (filename) DO NOTHING;
