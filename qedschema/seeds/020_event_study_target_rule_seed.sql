-- =============================================================================
-- Seed 020: target resolution ルール初期データ
-- 優先順位: candidate_code(10) > hypothesis_code(20) > tag_candidate(30)
--          > tag_hypothesis(40) > alias(50)
-- =============================================================================

INSERT INTO target_resolution_rules
    (rule_name, priority, match_strategy, source_field, target_table, target_id_col, target_code_col, description)
VALUES
    -- P1: report_metadata.candidate_code → factor_candidates.name
    ('candidate_code_direct', 10,
     'candidate_code', 'candidate_code',
     'factor_candidates', 'id', 'name',
     'report_metadata.candidate_code を factor_candidates.name で照合'),

    -- P2: report_metadata.hypothesis_code → hypotheses.title
    ('hypothesis_code_direct', 20,
     'hypothesis_code', 'hypothesis_code',
     'hypotheses', 'id', 'title',
     'report_metadata.hypothesis_code を hypotheses.title で照合'),

    -- P3: artifact_tag = "candidate:CODE"
    ('tag_candidate', 30,
     'tag_candidate', 'artifact_tag',
     'factor_candidates', 'id', 'name',
     'artifact_tag "candidate:CODE" prefix から factor_candidates.name で照合'),

    -- P4: artifact_tag = "hypothesis:CODE"
    ('tag_hypothesis', 40,
     'tag_hypothesis', 'artifact_tag',
     'hypotheses', 'id', 'title',
     'artifact_tag "hypothesis:CODE" prefix から hypotheses.title で照合')

ON CONFLICT (rule_name) DO UPDATE SET
    priority        = EXCLUDED.priority,
    match_strategy  = EXCLUDED.match_strategy,
    source_field    = EXCLUDED.source_field,
    target_table    = EXCLUDED.target_table,
    target_id_col   = EXCLUDED.target_id_col,
    target_code_col = EXCLUDED.target_code_col,
    description     = EXCLUDED.description,
    is_active       = true;
