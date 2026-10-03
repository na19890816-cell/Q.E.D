-- migration 085: frost_meta_validation
-- QED_REFACTORING_PLAN Phase 8 (P8) メタ検証結果の保存先
--
-- 1 行 = (meta_run_id, analysis_type, target, perturbation) の 1 測定。
--   analysis_type: threshold_sensitivity / axis_ablation / weight_perturbation
--   target       : ゲート名 / 重みフィールド名 / 'all_v1_weights'
--   perturbation : '+10%' / 'zero' / '±20%x50' など
-- 観測のみ。ここの結果に基づくポリシー変更は新 Note として登録する (P8 スコープ外)。
-- 冪等: 再実行は同一 meta_run_id で ON CONFLICT DO NOTHING。

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema='public' AND table_name='frost_meta_validation'
  ) THEN
    CREATE TABLE frost_meta_validation (
      id                  BIGSERIAL   PRIMARY KEY,
      meta_run_id         TEXT        NOT NULL,
      -- ↑ "meta:" + SHA-256(policy_hash|dataset_hash|params)[:32] — 同条件の再実行で同一
      policy_hash         TEXT        NOT NULL,
      dataset_hash        TEXT        NOT NULL,
      analysis_type       TEXT        NOT NULL
                          CHECK (analysis_type IN ('threshold_sensitivity','axis_ablation','weight_perturbation')),
      target              TEXT        NOT NULL,
      perturbation        TEXT        NOT NULL,
      n_candidates        INT         NOT NULL DEFAULT 0 CHECK (n_candidates >= 0),
      decision_flip_rate  DOUBLE PRECISION,
      gate_flip_rate      DOUBLE PRECISION,
      topk_jaccard        DOUBLE PRECISION,
      promo_jaccard       DOUBLE PRECISION,
      kendall_tau         DOUBLE PRECISION,
      kendall_tau_passed  DOUBLE PRECISION,
      metrics             JSONB       NOT NULL DEFAULT '{}',
      params              JSONB       NOT NULL DEFAULT '{}',
      created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
      CONSTRAINT uq_frost_meta_validation
        UNIQUE (meta_run_id, analysis_type, target, perturbation)
    );
    CREATE INDEX idx_frost_mv_policy   ON frost_meta_validation(policy_hash, created_at);
    CREATE INDEX idx_frost_mv_analysis ON frost_meta_validation(analysis_type, target);
    RAISE NOTICE 'created frost_meta_validation';
  ELSE
    RAISE NOTICE 'frost_meta_validation already exists — skipping';
  END IF;
END $$;
