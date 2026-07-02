-- migration 082: qed_policies
-- Phase 1 PolicySpec 統合 — ポリシースナップショット保存テーブル
--
-- 設計方針:
--   - policy_hash (SHA-256 hex) が主キー → 同一設定は一度だけ保存
--   - spec_json は PolicySpec.to_dict() の JSONB 直列化
--   - source_env_vars は load 時に参照した環境変数名の配列（再現性確保）
--   - used_at は frost_runs から参照される都度更新（最終利用時刻）
--   - engine_version, phase_tag で世代管理

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema='public' AND table_name='qed_policies'
  ) THEN
    CREATE TABLE qed_policies (
      policy_hash          TEXT        PRIMARY KEY,
      -- ↑ SHA-256(canonical JSON) の hex 文字列 (64 chars)

      spec_json            JSONB       NOT NULL,
      -- ↑ PolicySpec.to_dict() の完全スナップショット
      --   weights / hard_gates / selection / backtest / meta を含む

      source_env_vars      TEXT[]      NOT NULL DEFAULT '{}',
      -- ↑ このポリシーをロードした際に参照した環境変数名リスト
      --   再現のために記録（値は含まない）

      engine_version       TEXT        NOT NULL DEFAULT 'frost_v1',
      -- ↑ FrostConfig.engine_version と対応

      phase_tag            TEXT        NOT NULL DEFAULT 'phase1',
      -- ↑ QED リファクタリングフェーズ識別子

      description          TEXT,
      -- ↑ 任意の人間可読メモ（実験名・変更理由など）

      first_seen_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
      used_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
      created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE INDEX idx_qed_policies_engine   ON qed_policies(engine_version);
    CREATE INDEX idx_qed_policies_phase    ON qed_policies(phase_tag);
    CREATE INDEX idx_qed_policies_used_at  ON qed_policies(used_at DESC);

    RAISE NOTICE 'created qed_policies';
  ELSE
    RAISE NOTICE 'qed_policies already exists — skipping';
  END IF;
END $$;
