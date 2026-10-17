-- ADR-002: complete append-only protection for the original ledger and S3 receipts.
-- Prerequisites: 084 (tables/rejection function), then 086 (receipt table).
-- 087 is independent: it already protects snapshot captures from TRUNCATE.
-- Keep this additive so databases with 084/086 recorded also receive the fix.

DROP TRIGGER IF EXISTS trg_qed_trial_batches_no_truncate ON qed_trial_batches;
CREATE TRIGGER trg_qed_trial_batches_no_truncate
    BEFORE TRUNCATE ON qed_trial_batches
    FOR EACH STATEMENT EXECUTE FUNCTION qed_ledger_reject_mutation();

DROP TRIGGER IF EXISTS trg_qed_lineage_edges_no_truncate ON qed_lineage_edges;
CREATE TRIGGER trg_qed_lineage_edges_no_truncate
    BEFORE TRUNCATE ON qed_lineage_edges
    FOR EACH STATEMENT EXECUTE FUNCTION qed_ledger_reject_mutation();

DROP TRIGGER IF EXISTS trg_qed_manual_imports_no_truncate ON qed_manual_trial_imports;
CREATE TRIGGER trg_qed_manual_imports_no_truncate
    BEFORE TRUNCATE ON qed_manual_trial_imports
    FOR EACH STATEMENT EXECUTE FUNCTION qed_ledger_reject_mutation();

INSERT INTO _migrations(filename) VALUES ('088_qed_trial_ledger_no_truncate.sql')
    ON CONFLICT (filename) DO NOTHING;
