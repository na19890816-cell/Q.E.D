-- =============================================================================
-- Migration 017: experiment_reports → knowledge_artifacts ブリッジ
-- =============================================================================

CREATE TABLE IF NOT EXISTS knowledge_artifacts (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    artifact_id         TEXT        NOT NULL,           -- 外部参照用 slug (trace_id + seq)
    trace_id            TEXT        NOT NULL,
    source_run_id       TEXT,                           -- 元 run_id
    source_experiment_run_id UUID   REFERENCES experiment_runs(id),
    artifact_type       TEXT        NOT NULL DEFAULT 'event_study_report',
    artifact_tag        TEXT,                           -- candidate:CODE / hypothesis:CODE
    title               TEXT        NOT NULL,
    summary             TEXT        NOT NULL DEFAULT '',
    body_markdown       TEXT        NOT NULL DEFAULT '',
    metadata            JSONB       NOT NULL DEFAULT '{}',
    status              TEXT        NOT NULL DEFAULT 'draft',  -- draft|published|deprecated
    published_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT knowledge_artifacts_artifact_id_unique UNIQUE (artifact_id)
);

CREATE INDEX IF NOT EXISTS idx_ka_trace_id          ON knowledge_artifacts (trace_id);
CREATE INDEX IF NOT EXISTS idx_ka_artifact_type     ON knowledge_artifacts (artifact_type);
CREATE INDEX IF NOT EXISTS idx_ka_artifact_tag      ON knowledge_artifacts (artifact_tag);
CREATE INDEX IF NOT EXISTS idx_ka_status            ON knowledge_artifacts (status);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_ka_updated_at') THEN
        CREATE TRIGGER trg_ka_updated_at
            BEFORE UPDATE ON knowledge_artifacts
            FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
    END IF;
END;
$$;

INSERT INTO _migrations (filename, applied_at)
VALUES ('017_event_study_knowledge_artifact_bridge', now())
ON CONFLICT (filename) DO NOTHING;
