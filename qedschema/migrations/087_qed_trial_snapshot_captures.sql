-- ADR-002: freeze observed ledger inputs independently of live decision paths.
-- Requires 084's append-only trigger function. No existing table is altered.
CREATE TABLE IF NOT EXISTS qed_trial_snapshot_captures (
    capture_id TEXT PRIMARY KEY CHECK (capture_id ~ '^capture:[0-9a-f]{64}$'),
    captured_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    payload_sha256 TEXT NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object')
);

DROP TRIGGER IF EXISTS trg_qed_snapshot_captures_append_only ON qed_trial_snapshot_captures;
CREATE TRIGGER trg_qed_snapshot_captures_append_only
    BEFORE UPDATE OR DELETE ON qed_trial_snapshot_captures
    FOR EACH ROW EXECUTE FUNCTION qed_ledger_reject_mutation();

INSERT INTO _migrations(filename) VALUES ('087_qed_trial_snapshot_captures.sql')
    ON CONFLICT (filename) DO NOTHING;
