-- View: v_event_study_target_rule_resolution_status
CREATE OR REPLACE VIEW v_event_study_target_rule_resolution_status AS
SELECT
    trl.artifact_id,
    trl.trace_id,
    trl.resolution_status,
    trl.matched_rule_name,
    trl.matched_target_type,
    trl.matched_target_id,
    trl.matched_code,
    trl.candidate_count,
    trl.resolved_at,
    ka.title  AS artifact_title,
    ka.artifact_tag
FROM target_resolution_log trl
LEFT JOIN knowledge_artifacts ka ON ka.artifact_id = trl.artifact_id;
