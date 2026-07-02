-- migration 083: frost_runs に policy_hash 追加
-- Phase 1 PolicySpec 統合 — 実行単位にポリシー参照を追加
--
-- 設計方針:
--   - policy_hash は qed_policies(policy_hash) への外部キー参照（nullable）
--   - nullable にする理由: 既存 frost_runs レコードとの後方互換性
--   - Phase 1 以降の新規 run は必ず policy_hash を設定する
--   - 新規インデックスで「同じポリシーで実行した run 一覧」のクエリを高速化

DO $$ BEGIN
  -- policy_hash 列の追加
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema='public'
      AND table_name='frost_runs'
      AND column_name='policy_hash'
  ) THEN
    ALTER TABLE frost_runs
      ADD COLUMN policy_hash TEXT REFERENCES qed_policies(policy_hash) ON DELETE SET NULL;

    RAISE NOTICE 'added policy_hash to frost_runs';
  ELSE
    RAISE NOTICE 'frost_runs.policy_hash already exists — skipping';
  END IF;

  -- インデックス追加
  IF NOT EXISTS (
    SELECT 1 FROM pg_indexes
    WHERE schemaname='public'
      AND tablename='frost_runs'
      AND indexname='idx_frost_runs_policy_hash'
  ) THEN
    CREATE INDEX idx_frost_runs_policy_hash ON frost_runs(policy_hash)
      WHERE policy_hash IS NOT NULL;

    RAISE NOTICE 'created idx_frost_runs_policy_hash';
  ELSE
    RAISE NOTICE 'idx_frost_runs_policy_hash already exists — skipping';
  END IF;
END $$;
