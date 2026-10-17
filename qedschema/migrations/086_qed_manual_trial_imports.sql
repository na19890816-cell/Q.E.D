-- ADR-002 S3: independent append-only receipts. Existing decision paths unchanged.
CREATE TABLE IF NOT EXISTS qed_manual_trial_imports (
    source TEXT NOT NULL CHECK (length(btrim(source)) > 0),
    research_run_id TEXT NOT NULL CHECK (length(btrim(research_run_id)) > 0),
    batch_id TEXT NOT NULL UNIQUE REFERENCES qed_trial_batches(batch_id),
    evidence_sha256 TEXT NOT NULL UNIQUE CHECK (evidence_sha256 ~ '^[0-9a-f]{64}$'),
    inventory_sha256 TEXT NOT NULL UNIQUE CHECK (inventory_sha256 ~ '^[0-9a-f]{64}$'),
    n_trials INTEGER NOT NULL CHECK (n_trials > 0),
    completed_at TIMESTAMPTZ NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    submitted_by TEXT NOT NULL CHECK (length(btrim(submitted_by)) > 0),
    PRIMARY KEY (source, research_run_id),
    CHECK (completed_at <= recorded_at)
);

DROP TRIGGER IF EXISTS trg_qed_manual_imports_append_only ON qed_manual_trial_imports;
CREATE TRIGGER trg_qed_manual_imports_append_only
    BEFORE UPDATE OR DELETE ON qed_manual_trial_imports
    FOR EACH ROW EXECUTE FUNCTION qed_ledger_reject_mutation();

INSERT INTO _migrations(filename) VALUES ('086_qed_manual_trial_imports.sql')
    ON CONFLICT (filename) DO NOTHING;
