-- View: v_event_study_dashboard
CREATE OR REPLACE VIEW v_event_study_dashboard AS
SELECT
    r.run_id,
    r.trace_id,
    r.source_name,
    r.panel_kind,
    r.batch_label,
    r.status          AS run_status,
    r.total_events,
    r.started_at,
    r.completed_at,
    COUNT(s.id)       AS summary_count,
    AVG(s.car_from_t0)  AS avg_car,
    AVG(s.abnormal_return) AS avg_ar
FROM event_study_summary_runs r
LEFT JOIN event_study_summaries s ON s.run_id = r.run_id
GROUP BY r.run_id, r.trace_id, r.source_name, r.panel_kind,
         r.batch_label, r.status, r.total_events, r.started_at, r.completed_at;
