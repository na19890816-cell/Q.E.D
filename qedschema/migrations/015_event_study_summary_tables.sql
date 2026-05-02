-- =============================================================================
-- Migration 015: event_study_summary_runs / event_study_summaries
-- DuckDB → PostgreSQL writeback 受け皿テーブル
-- 冪等: IF NOT EXISTS / DO NOTHING
-- =============================================================================

-- ----------------------------------------------------------------------------
-- event_study_summary_runs: 1回の event study 実行ユニット
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS event_study_summary_runs (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id              TEXT        NOT NULL,           -- DuckDB 側の識別子 (batch_label + source_name)
    trace_id            TEXT        NOT NULL,           -- パイプライン横断 trace_id
    source_name         TEXT        NOT NULL,           -- EVENT_STUDY_SOURCE_NAME
    panel_kind          TEXT        NOT NULL,           -- abnormal_return | car
    batch_label         TEXT        NOT NULL,           -- EVENT_STUDY_BATCH_LABEL
    status              TEXT        NOT NULL DEFAULT 'pending',   -- pending|running|completed|failed
    total_events        INTEGER     NOT NULL DEFAULT 0,
    run_metadata        JSONB       NOT NULL DEFAULT '{}',
    started_at          TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT event_study_summary_runs_run_id_unique UNIQUE (run_id)
);

CREATE INDEX IF NOT EXISTS idx_esrun_trace_id        ON event_study_summary_runs (trace_id);
CREATE INDEX IF NOT EXISTS idx_esrun_batch_label     ON event_study_summary_runs (batch_label);
CREATE INDEX IF NOT EXISTS idx_esrun_status          ON event_study_summary_runs (status);

-- ----------------------------------------------------------------------------
-- event_study_summaries: イベント単位の集計結果
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS event_study_summaries (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id              TEXT        NOT NULL REFERENCES event_study_summary_runs(run_id) ON DELETE CASCADE,
    trace_id            TEXT        NOT NULL,
    benchmark_id        TEXT        NOT NULL,           -- DuckDB 出力の benchmark_id
    event_date          DATE,
    event_offset        INTEGER,                        -- t0 からの日数オフセット
    abnormal_return     NUMERIC,                        -- 異常リターン
    car_from_t0         NUMERIC,                        -- t0 からの CAR
    normal_return       NUMERIC,
    actual_return       NUMERIC,
    n_events            INTEGER,
    extra_metrics       JSONB       NOT NULL DEFAULT '{}',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT event_study_summaries_run_event_offset_unique
        UNIQUE (run_id, benchmark_id, event_offset)
);

CREATE INDEX IF NOT EXISTS idx_essummary_run_id      ON event_study_summaries (run_id);
CREATE INDEX IF NOT EXISTS idx_essummary_trace_id    ON event_study_summaries (trace_id);
CREATE INDEX IF NOT EXISTS idx_essummary_benchmark   ON event_study_summaries (benchmark_id);

-- 更新時刻自動更新トリガー関数（共通）
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_esrun_updated_at'
    ) THEN
        CREATE TRIGGER trg_esrun_updated_at
            BEFORE UPDATE ON event_study_summary_runs
            FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_essummary_updated_at'
    ) THEN
        CREATE TRIGGER trg_essummary_updated_at
            BEFORE UPDATE ON event_study_summaries
            FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
    END IF;
END;
$$;

-- migration 登録
INSERT INTO _migrations (filename, applied_at)
VALUES ('015_event_study_summary_tables', now())
ON CONFLICT (filename) DO NOTHING;
