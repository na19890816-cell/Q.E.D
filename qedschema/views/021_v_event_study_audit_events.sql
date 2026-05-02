-- View: v_event_study_audit_events
CREATE OR REPLACE VIEW v_event_study_audit_events AS
-- パイプライン固有監査テーブル
SELECT
    trace_id,
    phase           AS context,
    object_type,
    object_id,
    event_type,
    decision,
    decision_reason AS reason,
    metadata,
    created_at
FROM event_study_pipeline_audit
UNION ALL
-- QED本体 audit_events (event_study 関連)
SELECT
    trace_id,
    object_type     AS context,
    object_type,
    object_id,
    event_type,
    decision,
    decision_reason_code AS reason,
    metadata,
    created_at
FROM audit_events
WHERE object_type LIKE 'event_study%'
   OR (metadata->>'pipeline_phase') IS NOT NULL;
