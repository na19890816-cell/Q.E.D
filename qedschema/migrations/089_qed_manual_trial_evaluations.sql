-- S3 evaluation identities prevent subset/superset/replacement recounting.
-- Requires 084 and 086; never infer/backfill individual rows from legacy digests.
CREATE TABLE IF NOT EXISTS qed_manual_trial_evaluations (
    evaluation_sha256 TEXT PRIMARY KEY CHECK (evaluation_sha256 ~ '^[0-9a-f]{64}$'),
    batch_id TEXT NOT NULL REFERENCES qed_manual_trial_imports(batch_id)
);
CREATE INDEX IF NOT EXISTS idx_qed_manual_evaluations_batch
    ON qed_manual_trial_evaluations(batch_id);

DROP TRIGGER IF EXISTS trg_qed_manual_evaluations_append_only ON qed_manual_trial_evaluations;
CREATE TRIGGER trg_qed_manual_evaluations_append_only
    BEFORE UPDATE OR DELETE ON qed_manual_trial_evaluations
    FOR EACH ROW EXECUTE FUNCTION qed_ledger_reject_mutation();
DROP TRIGGER IF EXISTS trg_qed_manual_evaluations_no_truncate ON qed_manual_trial_evaluations;
CREATE TRIGGER trg_qed_manual_evaluations_no_truncate
    BEFORE TRUNCATE ON qed_manual_trial_evaluations
    FOR EACH STATEMENT EXECUTE FUNCTION qed_ledger_reject_mutation();

INSERT INTO _migrations(filename) VALUES ('089_qed_manual_trial_evaluations.sql')
    ON CONFLICT (filename) DO NOTHING;
