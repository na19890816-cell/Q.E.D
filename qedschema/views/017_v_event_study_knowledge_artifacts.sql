-- View: v_event_study_knowledge_artifacts
CREATE OR REPLACE VIEW v_event_study_knowledge_artifacts AS
SELECT
    ka.artifact_id,
    ka.trace_id,
    ka.source_run_id,
    ka.artifact_type,
    ka.artifact_tag,
    ka.title,
    ka.summary,
    ka.status,
    ka.published_at,
    al.target_type,
    al.target_id,
    al.target_code,
    al.resolution_method,
    trl.resolution_status
FROM knowledge_artifacts ka
LEFT JOIN artifact_links al ON al.artifact_id = ka.artifact_id AND al.link_status = 'active'
LEFT JOIN target_resolution_log trl ON trl.artifact_id = ka.artifact_id;
