-- migration 081: frost_partitioning_prep
-- FROST テーブルのパーティショニング準備
-- 目的: 長期運用時の大量データ管理
-- 方針: 現行テーブルを変更せず、将来の partitioned table への移行準備のみ
-- rerun-safe: DO$$ + IF NOT EXISTS

-- ---------------------------------------------------------------------------
-- 1. パーティショニング管理テーブル
--    どのテーブルがどのキーでパーティション対象かを記録
-- ---------------------------------------------------------------------------
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = 'public'
      AND table_name = 'frost_partition_registry'
  ) THEN
    CREATE TABLE frost_partition_registry (
      registry_id        SERIAL      PRIMARY KEY,
      table_name         TEXT        NOT NULL UNIQUE,
      partition_key      TEXT        NOT NULL,
      partition_type     TEXT        NOT NULL DEFAULT 'RANGE'
                         CHECK (partition_type IN ('RANGE', 'LIST', 'HASH')),
      partition_interval TEXT        NOT NULL DEFAULT '1 month',
      oldest_partition   DATE,
      newest_partition   DATE,
      partition_count    INT         NOT NULL DEFAULT 0,
      status             TEXT        NOT NULL DEFAULT 'pending'
                         CHECK (status IN ('pending', 'migrating', 'active', 'retired')),
      notes              TEXT,
      created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    COMMENT ON TABLE frost_partition_registry IS
      'FROST テーブルパーティショニング管理レジストリ。
       status=pending: 移行未着手
       status=migrating: 移行中
       status=active: パーティション有効
       status=retired: 非推奨/廃止';
  END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 2. パーティション対象テーブルの候補登録
-- ---------------------------------------------------------------------------
INSERT INTO frost_partition_registry
  (table_name, partition_key, partition_type, partition_interval, status, notes)
VALUES
  ('frost_evaluations',
   'created_at',
   'RANGE',
   '3 months',
   'pending',
   'frost_score 大量生成時の主要テーブル。3ヶ月単位でパーティション推奨。'),
  ('frost_selection_decisions',
   'decided_at',
   'RANGE',
   '3 months',
   'pending',
   '決定履歴テーブル。監査目的で長期保持が必要。'),
  ('frost_runs',
   'started_at',
   'RANGE',
   '6 months',
   'pending',
   '実行ログ。6ヶ月単位で十分。'),
  ('eml_backtest_folds',
   'created_at',
   'RANGE',
   '1 month',
   'pending',
   '最大テーブル。候補 × fold 数 の爆発的増加に注意。月次パーティション推奨。'),
  ('eml_alpha_candidates',
   'created_at',
   'RANGE',
   '6 months',
   'pending',
   'EML 候補テーブル。6ヶ月単位で安定。')
ON CONFLICT (table_name) DO NOTHING;

-- ---------------------------------------------------------------------------
-- 3. パーティション移行手順ビュー（DBA 参照用）
-- ---------------------------------------------------------------------------
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.views
    WHERE table_schema = 'public'
      AND table_name = 'frost_partition_migration_guide'
  ) THEN
    CREATE VIEW frost_partition_migration_guide AS
    SELECT
      table_name,
      partition_key,
      partition_type,
      partition_interval,
      status,
      notes,
      '-- Step 1: 新しいパーティションテーブルを作成' || chr(10) ||
      'CREATE TABLE ' || table_name || '_partitioned' || chr(10) ||
      '  (LIKE ' || table_name || ' INCLUDING ALL)' || chr(10) ||
      '  PARTITION BY ' || partition_type || ' (' || partition_key || ');' || chr(10) ||
      '-- Step 2: データ移行 (バックグラウンド)' || chr(10) ||
      'INSERT INTO ' || table_name || '_partitioned SELECT * FROM ' || table_name || ';' || chr(10) ||
      '-- Step 3: テーブル名スワップ' || chr(10) ||
      'ALTER TABLE ' || table_name || ' RENAME TO ' || table_name || '_old;' || chr(10) ||
      'ALTER TABLE ' || table_name || '_partitioned RENAME TO ' || table_name || ';'
        AS migration_script_hint
    FROM frost_partition_registry
    WHERE status IN ('pending', 'migrating');
  END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 4. データ量監視テーブル（パーティション決定の根拠となる統計）
-- ---------------------------------------------------------------------------
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = 'public'
      AND table_name = 'frost_table_size_log'
  ) THEN
    CREATE TABLE frost_table_size_log (
      log_id       SERIAL      PRIMARY KEY,
      table_name   TEXT        NOT NULL,
      row_count    BIGINT      NOT NULL DEFAULT 0,
      size_bytes   BIGINT      NOT NULL DEFAULT 0,
      index_bytes  BIGINT      NOT NULL DEFAULT 0,
      measured_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE INDEX idx_frost_size_log_table_measured
      ON frost_table_size_log (table_name, measured_at DESC);

    COMMENT ON TABLE frost_table_size_log IS
      'FROST テーブルのサイズ推移ログ。
       パーティション移行タイミング判断に使用。
       目安: row_count > 1,000,000 または size_bytes > 1GB でパーティション検討。';
  END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 5. テーブルサイズ計測関数
-- ---------------------------------------------------------------------------
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_proc p
    JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = 'public'
      AND p.proname = 'frost_log_table_sizes'
  ) THEN
    CREATE FUNCTION frost_log_table_sizes()
    RETURNS VOID
    LANGUAGE plpgsql
    AS $$
    DECLARE
      tbl TEXT;
      target_tables TEXT[] := ARRAY[
        'frost_evaluations',
        'frost_selection_decisions',
        'frost_runs',
        'frost_fitness_candidates',
        'eml_backtest_folds',
        'eml_alpha_candidates',
        'eml_alpha_evaluations'
      ];
    BEGIN
      FOREACH tbl IN ARRAY target_tables LOOP
        BEGIN
          INSERT INTO frost_table_size_log (table_name, row_count, size_bytes, index_bytes)
          SELECT
            tbl,
            COALESCE((SELECT reltuples::BIGINT FROM pg_class WHERE relname = tbl), 0),
            COALESCE(pg_relation_size(tbl::regclass), 0),
            COALESCE(pg_indexes_size(tbl::regclass), 0);
        EXCEPTION WHEN undefined_table THEN
          -- テーブルが存在しない場合はスキップ
          NULL;
        END;
      END LOOP;
    END;
    $$;

    COMMENT ON FUNCTION frost_log_table_sizes() IS
      'FROST 関連テーブルのサイズを frost_table_size_log に記録する。
       推奨: cron で 1 日 1 回実行。
       SELECT frost_log_table_sizes();';
  END IF;
END $$;

-- migration 081 完了
-- 次のステップ:
--   1. frost_log_table_sizes() を定期実行 (pg_cron 等)
--   2. frost_table_size_log でサイズ増加を監視
--   3. row_count > 1,000,000 になったら frost_partition_registry の status='migrating' に更新
--   4. frost_partition_migration_guide の migration_script_hint を参照して移行実施
