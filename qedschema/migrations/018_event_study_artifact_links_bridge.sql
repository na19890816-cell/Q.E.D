-- =============================================================================
-- Migration 018: artifact_links ブリッジ
-- knowledge_artifacts → 解決済み target (factor_candidates / hypotheses) へのリンク
-- =============================================================================

CREATE TABLE IF NOT EXISTS artifact_links (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    artifact_id         TEXT        NOT NULL REFERENCES knowledge_artifacts(artifact_id) ON DELETE CASCADE,
    trace_id            TEXT        NOT NULL,
    target_type         TEXT        NOT NULL,           -- factor_candidate | hypothesis
    target_id           UUID        NOT NULL,           -- 解決済み UUID
    target_code         TEXT,                           -- 解決に使ったコード値
    resolution_method   TEXT        NOT NULL,           -- candidate_code|hypothesis_code|tag_candidate|tag_hypothesis
    link_status         TEXT        NOT NULL DEFAULT 'active',   -- active|deprecated
    metadata            JSONB       NOT NULL DEFAULT '{}',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT artifact_links_artifact_target_unique UNIQUE (artifact_id, target_type, target_id)
);

CREATE INDEX IF NOT EXISTS idx_al_artifact_id   ON artifact_links (artifact_id);
CREATE INDEX IF NOT EXISTS idx_al_trace_id      ON artifact_links (trace_id);
CREATE INDEX IF NOT EXISTS idx_al_target_id     ON artifact_links (target_id);
CREATE INDEX IF NOT EXISTS idx_al_target_type   ON artifact_links (target_type);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_al_updated_at') THEN
        CREATE TRIGGER trg_al_updated_at
            BEFORE UPDATE ON artifact_links
            FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
    END IF;
END;
$$;

INSERT INTO _migrations (filename, applied_at)
VALUES ('018_event_study_artifact_links_bridge', now())
ON CONFLICT (filename) DO NOTHING;
