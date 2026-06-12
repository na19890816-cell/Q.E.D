-- migration 073: causal_promotion_gate
-- Causal Discovery Gate の昇格判定記録テーブル
-- rerun-safe: DO$$ + IF NOT EXISTS

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = 'public'
      AND table_name = 'causal_promotion_gate'
  ) THEN
    CREATE TABLE causal_promotion_gate (
      gate_id                   UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
      candidate_id              TEXT        NOT NULL,
      run_id                    TEXT        NOT NULL,
      trace_id                  TEXT        NOT NULL,
      -- Gate 判定結果
      direction_gate_pass       BOOLEAN     NOT NULL DEFAULT TRUE,
      invariance_gate_pass      BOOLEAN     NOT NULL DEFAULT TRUE,
      composite_gate_pass       BOOLEAN     NOT NULL DEFAULT TRUE,
      overall_gate_pass         BOOLEAN     NOT NULL DEFAULT TRUE,
      -- 閾値（記録用）
      min_direction_score_used  FLOAT       NOT NULL DEFAULT 0.60,
      min_invariance_ratio_used FLOAT       NOT NULL DEFAULT 0.70,
      -- スコア（記録用）
      causal_composite_score    FLOAT       NOT NULL DEFAULT 0.0,
      intervention_consistency  FLOAT       NOT NULL DEFAULT 0.0,
      confounding_risk          FLOAT       NOT NULL DEFAULT 0.0,
      -- Gate 理由
      gate_reasons_json         JSONB       NOT NULL DEFAULT '[]',
      -- メタ
      dry_run                   BOOLEAN     NOT NULL DEFAULT FALSE,
      decided_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
      created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
      CONSTRAINT uq_causal_gate_cand_run UNIQUE (candidate_id, run_id)
    );

    CREATE INDEX idx_causal_gate_candidate ON causal_promotion_gate (candidate_id);
    CREATE INDEX idx_causal_gate_run       ON causal_promotion_gate (run_id);
    CREATE INDEX idx_causal_gate_trace     ON causal_promotion_gate (trace_id);
    CREATE INDEX idx_causal_gate_overall   ON causal_promotion_gate (overall_gate_pass);
    CREATE INDEX idx_causal_gate_fail      ON causal_promotion_gate (overall_gate_pass)
      WHERE overall_gate_pass = FALSE;

    COMMENT ON TABLE causal_promotion_gate IS
      'Causal Discovery Layer: 昇格 Gate 判定記録。
       overall_gate_pass=FALSE の候補は FROST v2 で REJECTED になる可能性がある。
       Manual review gate の前段として機能する。';
  END IF;
END $$;

-- migration 073 完了
