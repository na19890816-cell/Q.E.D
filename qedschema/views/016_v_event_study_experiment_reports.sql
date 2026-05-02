-- View: v_event_study_experiment_reports
CREATE OR REPLACE VIEW v_event_study_experiment_reports AS
SELECT
    b.run_id,
    b.trace_id,
    b.report_title,
    b.report_summary,
    b.promotion_status,
    b.promoted_at,
    r.id              AS experiment_run_id,
    r.experiment_type,
    r.status          AS experiment_status,
    r.result_summary
FROM event_study_experiment_report_bridge b
LEFT JOIN experiment_runs r ON r.id = b.experiment_run_id;
