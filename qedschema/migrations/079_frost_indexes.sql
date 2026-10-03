-- migration 079: frost_indexes
-- FROST テーブル群への複合インデックス追加
-- パフォーマンス: 候補数増加時のクエリ高速化
-- rerun-safe: IF NOT EXISTS / CREATE INDEX CONCURRENTLY 相当を DO$$ ブロックで保護

-- ---------------------------------------------------------------------------
-- frost_evaluations 複合インデックス
-- ---------------------------------------------------------------------------
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_indexes
    WHERE indexname = 'idx_frost_eval_run_candidate'
  ) THEN
    CREATE INDEX idx_frost_eval_run_candidate
      ON frost_evaluations (run_id, candidate_id);
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_indexes
    WHERE indexname = 'idx_frost_eval_trace_created'
  ) THEN
    CREATE INDEX idx_frost_eval_trace_created
      ON frost_evaluations (trace_id, created_at DESC);
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_indexes
    WHERE indexname = 'idx_frost_eval_frost_score'
  ) THEN
    CREATE INDEX idx_frost_eval_frost_score
      ON frost_evaluations (frost_score DESC NULLS LAST);
  END IF;
END $$;

-- ---------------------------------------------------------------------------
-- frost_selection_decisions 複合インデックス
-- ---------------------------------------------------------------------------
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_indexes
    WHERE indexname = 'idx_frost_dec_decision_promotion'
  ) THEN
    CREATE INDEX idx_frost_dec_decision_promotion
      ON frost_selection_decisions (decision, promotion_eligible);
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_indexes
    WHERE indexname = 'idx_frost_dec_run_decision'
  ) THEN
    CREATE INDEX idx_frost_dec_run_decision
      ON frost_selection_decisions (run_id, decision);
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_indexes
    WHERE indexname = 'idx_frost_dec_trace_created'
  ) THEN
    CREATE INDEX idx_frost_dec_trace_created
      ON frost_selection_decisions (trace_id, created_at DESC);
  END IF;
END $$;

-- ---------------------------------------------------------------------------
-- frost_fitness_candidates 複合インデックス
-- ---------------------------------------------------------------------------
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_indexes
    WHERE indexname = 'idx_frost_cand_run_created'
  ) THEN
    -- 修正 (2026-10-03): 旧定義は存在しない列 (batch_label, run_date) を参照しており適用不能だった。
    -- batch_label は frost_runs 側の列のため、候補は run_id + created_at で引く。
    CREATE INDEX idx_frost_cand_run_created
      ON frost_fitness_candidates (run_id, created_at DESC);
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_indexes
    WHERE indexname = 'idx_frost_cand_source_type_trace'
  ) THEN
    -- 修正 (2026-10-03): 実列名は source_system ではなく source_type
    CREATE INDEX idx_frost_cand_source_type_trace
      ON frost_fitness_candidates (source_type, trace_id);
  END IF;
END $$;

-- ---------------------------------------------------------------------------
-- frost_runs 複合インデックス
-- ---------------------------------------------------------------------------
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_indexes
    WHERE indexname = 'idx_frost_runs_status_started'
  ) THEN
    CREATE INDEX idx_frost_runs_status_started
      ON frost_runs (status, started_at DESC);
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_indexes
    WHERE indexname = 'idx_frost_runs_batch_label'
  ) THEN
    CREATE INDEX idx_frost_runs_batch_label
      ON frost_runs (batch_label, started_at DESC);
  END IF;
END $$;

-- ---------------------------------------------------------------------------
-- eml_backtest_folds 複合インデックス（将来の大量 fold 対策）
-- ---------------------------------------------------------------------------
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_indexes
    WHERE indexname = 'idx_eml_backtest_folds_run_candidate'
  ) THEN
    -- 修正 (2026-10-03): eml_backtest_folds に run_id 列は無い (backtest_run_id が実列)
    CREATE INDEX idx_eml_backtest_folds_run_candidate
      ON eml_backtest_folds (backtest_run_id, candidate_id);
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_indexes
    WHERE indexname = 'idx_eml_backtest_folds_trace_created'
  ) THEN
    CREATE INDEX idx_eml_backtest_folds_trace_created
      ON eml_backtest_folds (trace_id, created_at DESC);
  END IF;
END $$;

-- ---------------------------------------------------------------------------
-- eml_alpha_candidates JSONB GIN インデックス（run_metadata 全文検索用）
-- ---------------------------------------------------------------------------
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_indexes
    WHERE indexname = 'idx_eml_alpha_candidates_run_metadata_gin'
  ) THEN
    CREATE INDEX idx_eml_alpha_candidates_run_metadata_gin
      ON eml_alpha_candidates USING GIN (run_metadata);
  END IF;
END $$;

-- migration 079 完了
