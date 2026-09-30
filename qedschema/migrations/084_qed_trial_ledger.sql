-- migration 084: qed_trial_batches / qed_lineage_edges
-- ADR-002 系譜ログ (B 設計) — append-only 試行台帳
--
-- 設計方針:
--   - 試行 1 件 = 1 行にはしない。探索 1 回 (batch) = 1 行で件数 + SR 十分統計量を保持
--   - append-only: UPDATE / DELETE はトリガで拒否 (改竄不能 / as-of 再現性)
--   - 冪等: batch_id / edge_id は決定論的 UUID5 → 再実行は ON CONFLICT DO NOTHING
--   - 既存テーブルへの変更なし (追加とビューのみ)

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema='public' AND table_name='qed_trial_batches'
  ) THEN
    CREATE TABLE qed_trial_batches (
      batch_id        TEXT        PRIMARY KEY,
      -- ↑ UUID5(namespace, "run_id|stage|seq") — 再実行で同一
      family_key      TEXT        NOT NULL,
      -- ↑ "fam:" + SHA-256(canonical{horizon, universe, target, terminal_set_hash})[:32]
      run_id          TEXT        NOT NULL,
      trace_id        TEXT        NOT NULL DEFAULT '',
      source_type     TEXT        NOT NULL DEFAULT 'eml',
      stage           TEXT        NOT NULL
                      CHECK (stage IN ('exhaustive','gradient','manual','frost_eval','external')),
      n_trials        INT         NOT NULL CHECK (n_trials >= 0),
      -- ↑ fitness を計算した試行数 (保存しない候補も含む)
      sr_count        INT         NOT NULL DEFAULT 0 CHECK (sr_count >= 0 AND sr_count <= n_trials),
      sr_mean         DOUBLE PRECISION NOT NULL DEFAULT 0.0,
      sr_m2           DOUBLE PRECISION NOT NULL DEFAULT 0.0 CHECK (sr_m2 >= 0.0),
      -- ↑ Welford 十分統計量 (非年率 SR)。並列合成で V[SR] を再構成する
      sr_periodicity  TEXT        NOT NULL DEFAULT 'daily',
      family_spec     JSONB       NOT NULL DEFAULT '{}',
      metadata        JSONB       NOT NULL DEFAULT '{}',
      recorded_at     TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE INDEX idx_qed_tb_family      ON qed_trial_batches(family_key, recorded_at);
    CREATE INDEX idx_qed_tb_run         ON qed_trial_batches(run_id);
    CREATE INDEX idx_qed_tb_trace       ON qed_trial_batches(trace_id);
    RAISE NOTICE 'created qed_trial_batches';
  ELSE
    RAISE NOTICE 'qed_trial_batches already exists — skipping';
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema='public' AND table_name='qed_lineage_edges'
  ) THEN
    CREATE TABLE qed_lineage_edges (
      edge_id              TEXT        PRIMARY KEY,
      parent_family_key    TEXT        NOT NULL,
      child_family_key     TEXT        NOT NULL,
      parent_formula_hash  TEXT,
      child_formula_hash   TEXT,
      relation             TEXT        NOT NULL
                           CHECK (relation IN ('mutation','retrain','param_tweak',
                                               'manual_edit','ensemble_member')),
      run_id               TEXT        NOT NULL DEFAULT '',
      metadata             JSONB       NOT NULL DEFAULT '{}',
      recorded_at          TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE INDEX idx_qed_le_child   ON qed_lineage_edges(child_family_key, recorded_at);
    CREATE INDEX idx_qed_le_parent  ON qed_lineage_edges(parent_family_key);
    CREATE INDEX idx_qed_le_cformula ON qed_lineage_edges(child_formula_hash)
      WHERE child_formula_hash IS NOT NULL;
    RAISE NOTICE 'created qed_lineage_edges';
  ELSE
    RAISE NOTICE 'qed_lineage_edges already exists — skipping';
  END IF;
END $$;

-- ---------------------------------------------------------------------------
-- append-only 強制トリガ (ADR-002 R5)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION qed_ledger_reject_mutation() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'ADR-002: % is append-only (% rejected)', TG_TABLE_NAME, TG_OP;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_qed_trial_batches_append_only ON qed_trial_batches;
CREATE TRIGGER trg_qed_trial_batches_append_only
  BEFORE UPDATE OR DELETE ON qed_trial_batches
  FOR EACH ROW EXECUTE FUNCTION qed_ledger_reject_mutation();

DROP TRIGGER IF EXISTS trg_qed_lineage_edges_append_only ON qed_lineage_edges;
CREATE TRIGGER trg_qed_lineage_edges_append_only
  BEFORE UPDATE OR DELETE ON qed_lineage_edges
  FOR EACH ROW EXECUTE FUNCTION qed_ledger_reject_mutation();

-- ---------------------------------------------------------------------------
-- family 単位の累計ビュー (系譜遡及は含まない。遡及は frost_lineage.TrialLedger で行う)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_qed_family_trial_totals AS
SELECT
  family_key,
  COUNT(*)            AS batch_count,
  SUM(n_trials)       AS n_trials_total,
  SUM(sr_count)       AS sr_count_total,
  MIN(recorded_at)    AS first_recorded_at,
  MAX(recorded_at)    AS last_recorded_at
FROM qed_trial_batches
GROUP BY family_key;

INSERT INTO _migrations(filename) VALUES('084_qed_trial_ledger.sql')
  ON CONFLICT(filename) DO NOTHING;
SELECT '--- OK ---';
