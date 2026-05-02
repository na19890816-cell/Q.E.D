-- =============================================================================
-- Migration 020: target rule auto resolution テーブル
-- candidate.code / hypothesis.code 自動解決ルールと解決状態管理
-- =============================================================================

-- target_resolution_rules: 解決ルール定義
CREATE TABLE IF NOT EXISTS target_resolution_rules (
    id              UUID    PRIMARY KEY DEFAULT gen_random_uuid(),
    rule_name       TEXT    NOT NULL,
    priority        INTEGER NOT NULL DEFAULT 100,  -- 小さいほど優先
    match_strategy  TEXT    NOT NULL,              -- candidate_code|hypothesis_code|tag_candidate|tag_hypothesis|alias
    source_field    TEXT    NOT NULL,              -- report_metadata のキー名 or tag prefix
    target_table    TEXT    NOT NULL,              -- factor_candidates | hypotheses
    target_id_col   TEXT    NOT NULL DEFAULT 'id',
    target_code_col TEXT    NOT NULL,              -- 照合する列名 (name / title 等)
    is_active       BOOLEAN NOT NULL DEFAULT true,
    description     TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT trr_rule_name_unique UNIQUE (rule_name)
);

-- target_resolution_log: 解決実行ログ（unresolved / ambiguous も記録）
CREATE TABLE IF NOT EXISTS target_resolution_log (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    artifact_id         TEXT        NOT NULL,
    trace_id            TEXT        NOT NULL,
    resolution_status   TEXT        NOT NULL,      -- resolved|unresolved|ambiguous
    matched_rule_name   TEXT,
    matched_target_id   UUID,
    matched_target_type TEXT,
    matched_code        TEXT,
    candidate_count     INTEGER     NOT NULL DEFAULT 0,
    resolution_detail   JSONB       NOT NULL DEFAULT '{}',
    resolved_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT trl_artifact_id_unique UNIQUE (artifact_id)
);

CREATE INDEX IF NOT EXISTS idx_trl_trace_id             ON target_resolution_log (trace_id);
CREATE INDEX IF NOT EXISTS idx_trl_resolution_status    ON target_resolution_log (resolution_status);

INSERT INTO _migrations (filename, applied_at)
VALUES ('020_event_study_target_rule_auto_resolution', now())
ON CONFLICT (filename) DO NOTHING;
