# prostock — EML Alpha Discovery & FROST Meta-Fitness Engine

**プロジェクト**: Q.E.D. EML Alpha Discovery / Backtest / FROST Phase 5 Selection Pipeline  
**ステータス**: ✅ 実装完了・全テスト通過 (278 passed)  
**最終更新**: 2026-06-10

---

## プロジェクト概要

イベントスタディパネルデータからシンボリック回帰（EML）を用いてアルファ候補を  
自動探索・評価・バックテスト・Q.E.D. チェーンへ昇格させるパイプラインです。

### アーキテクチャ

```
event_study_summaries  (PostgreSQL)
          │
          ▼  Phase A — ターミナルセット構築
    [r1, r5, r20, gap, vol, ...]
          │
          ▼  Phase B — EML 探索 (exhaustive + gradient)
    [EMLCandidate]  ×22 candidates
          │
          ▼  Phase C — DB 書き込み
    [eml_alpha_runs]
    [eml_alpha_candidates]
          │
          ▼  Phase D — Walk-forward バックテスト
    [eml_backtest_runs]
    [eml_backtest_folds]
          │
          ▼  Phase E — Q.E.D. プロモーション
    [eml_alpha_promotion_bridge]
    [knowledge_artifacts]
    [audit_events]
          │
          ▼  Phase 5 — FROST Meta-Fitness Engine  ← NEW
    [frost_fitness_candidates]
    [frost_evaluations]          (10軸スコア)
    [frost_selection_decisions]  (SELECTED/HOLD/REJECTED/REVIEW_REQUIRED)
    [frost_promotion_bridges]    (Q.E.D. 昇格 bridge)
    [frost_audit_event_bridges]  (4-status audit)
```

---

## 実装済み機能

| レイヤー | モジュール | 概要 |
|----------|-----------|------|
| EML Core | `analytics/python/alpha/eml/eml_core.py` | EMLNode ツリー構造、NaN/Inf セーフ to_json() |
| EML Tree | `analytics/python/alpha/eml/eml_tree.py` | ツリー生成・snap・prune |
| EML Compiler | `analytics/python/alpha/eml/eml_compiler.py` | 安全式コンパイル・validate |
| EML Search | `analytics/python/alpha/eml/eml_search.py` | exhaustive + gradient (Adam) 探索 |
| EML Fitness | `analytics/python/alpha/eml/eml_fitness.py` | 複合フィットネス関数 (rank_IC × 6指標) |
| EML Runtime | `analytics/python/alpha/eml/eml_runtime_lower.py` | ランク正規化シグナル生成 |
| EML Evaluator | `analytics/python/alpha/eml/eml_evaluation_runner.py` | 5指標グループ評価 |
| EML Master | `analytics/python/alpha/eml/eml_master_formula.py` | パイプライン全体オーケストレーション |
| Metrics | `analytics/python/metrics/{predictive,portfolio,trading,risk,regime}.py` | 予測・ポートフォリオ・取引・リスク・レジーム指標 |
| Backtest | `analytics/python/backtest/harness.py` | Walk-forward (expanding/rolling) |
| Cost/Slippage | `analytics/python/backtest/{cost_model,slippage_model}.py` | コスト・スリッページモデル |
| Risk Gate | `analytics/python/backtest/risk_gate.py` | クライシス/ボラティリティ gate |
| Features | `analytics/python/features/build_terminal_set.py` | ターミナルセット構築 |
| Regime | `analytics/python/features/regime_features.py` | クライシスマスク生成 |
| IO: Alpha | `analytics/python/io/postgres_eml_alpha_writer.py` | eml_alpha_runs / candidates UPSERT |
| IO: Backtest | `analytics/python/io/postgres_eml_backtest_writer.py` | eml_backtest_runs / folds UPSERT |
| IO: Eval | `analytics/python/io/postgres_eml_evaluation_writer.py` | eml_alpha_evaluations UPSERT |
| Promotion | `analytics/python/alpha/promotion_bridge.py` | Q.E.D. チェーン昇格 (bridge/KA/audit) |

---

## データモデル

### EML テーブル (PostgreSQL)

| テーブル | 主キー | 概要 |
|----------|--------|------|
| `eml_alpha_runs` | `run_id` | 探索実行メタデータ |
| `eml_alpha_candidates` | `candidate_id` | 探索済みアルファ候補 |
| `eml_backtest_runs` | `backtest_run_id` | バックテスト実行結果 |
| `eml_backtest_folds` | `fold_id` | ウォークフォワード fold 詳細 |
| `eml_alpha_promotion_bridge` | `candidate_id` | Q.E.D. 昇格ブリッジ |
| `knowledge_artifacts` | `artifact_id` | 昇格アルファの知識アーティファクト |
| `audit_events` | `id` (uuid) | APPLIED / REJECTED / DRY_RUN 監査ログ |

### EML ツリー文法

```
S → 1 | t_i | eml(S, S)
eml(a, b) = a  if sigmoid(raw_weight) >= 0.5  (snap後)
           = b  otherwise
depth: 2 ≦ depth ≦ 4
```

### フィットネス関数

```
fitness = 0.30 × rank_IC
        + 0.30 × cost_adjusted_sharpe
        + 0.20 × regime_consistency_score
        − 0.10 × turnover_penalty
        − 0.05 × complexity_penalty
        − 0.05 × drawdown_penalty
```

---

## テスト状況

### ユニットテスト (229 passed)

| ファイル | テスト数 | カバレッジ |
|----------|----------|-----------|
| `test_eml_layers.py` | 65 | EML全レイヤー (Core/Tree/Metrics/Backtest/Search/IO/Master) |
| `test_eml_core.py` | 12 | EMLNode, depth, compiler |
| `test_audit_payload.py` | 14 | audit_events ペイロード検証 |
| `test_event_study_writer.py` | 12 | EventStudyWriter UPSERT |
| `test_target_rule_resolver.py` | 17 | TargetRuleResolver |
| `test_trace_id_consistency.py` | 13 | trace_id 伝播・一貫性 |
| `test_frost_layers.py` | **56** | FROST全レイヤー (Config/Contracts/Features/Metrics/Stability/PBO/Selector/Ranker/Decision/Report) |

### 統合テスト (49 passed)

| ファイル | テスト数 | カバレッジ |
|----------|----------|-----------|
| `test_eml_pipeline_integration.py` | 25 | Phase A→E E2E、DB UPSERT安全性、NaN/Infガード、dry_run動作 |
| `test_pipeline_integration.py` | — | Event Study Pipeline Phase A→D |
| `test_frost_integration.py` | **24** | FROST E2E (Writer/Runner/HardGates/Bridges/DataQuality/Report) |

**合計: 278 passed**

---

## 実行方法

### 環境変数設定

```bash
export QED_PG_DSN="postgresql://postgres:postgres@localhost:5432/qed_dev"
export EML_ALPHA_ENABLED=1
export PYTHONPATH=/home/user/prostock
```

### フルパイプライン実行

```bash
# Makefile 経由
make eml-pipeline

# dry-run
make eml-pipeline-dry

# ステータス確認
make eml-status
```

### スクリプト直接実行

```bash
# 標準実行
python scripts/postgres/run_eml_pipeline.py

# パラメータ指定
EML_ALPHA_MAX_DEPTH=3 \
EML_TERMINAL_SET=r1,r5,r20,gap,vol \
EML_ALPHA_MIN_FITNESS=-0.3 \
python scripts/postgres/run_eml_pipeline.py
```

### テスト実行

```bash
# ユニットテストのみ (DB不要)
python -m pytest tests/unit/ -v

# 統合テスト (PostgreSQL必要)
QED_PG_DSN="..." python -m pytest tests/integration/ -v

# 全テスト
QED_PG_DSN="..." python -m pytest tests/ -v
```

---

## Makefile ターゲット一覧 (EML)

| ターゲット | 説明 |
|-----------|------|
| `make eml-terminal` | Phase A: ターミナルセット確認 |
| `make eml-search` | Phase B: EML探索実行 |
| `make eml-pipeline` | Phase A→E フルパイプライン |
| `make eml-pipeline-dry` | Phase A→E dry-run |
| `make eml-backtest-summary` | 直近バックテスト結果表示 |
| `make eml-status` | DBテーブルカウント確認 |

## Makefile ターゲット一覧 (FROST)

| ターゲット | 説明 |
|-----------|------|
| `make frost-init` | migrations + views を DB に適用 |
| `make frost-init-dry` | 適用の dry-run 確認 |
| `make frost-pipeline` | FROST 選抜パイプライン実行 |
| `make frost-pipeline-dry` | パイプライン dry-run |
| `make frost-backfill` | EML 候補の遡及評価 |
| `make frost-backfill-dry` | 遡及評価 dry-run |
| `make frost-promote` | 承認済み候補を Q.E.D. へ昇格 |
| `make frost-promote-dry` | 昇格 dry-run |
| `make frost-verify` | テーブル存在・件数・品質の全体検証 |
| `make frost-status` | 実行ステータス確認 |
| `make frost-clean` | ローカルキャッシュ削除 |

---

## 環境変数一覧 (EML)

| 変数名 | デフォルト | 説明 |
|--------|-----------|------|
| `QED_PG_DSN` | — | PostgreSQL DSN (必須) |
| `EML_ALPHA_ENABLED` | `0` | `1` で有効化 |
| `EML_ALPHA_DRY_RUN` | `0` | `1` で dry-run モード |
| `EML_ALPHA_MAX_DEPTH` | `3` | EML ツリー最大深度 (2-4) |
| `EML_TERMINAL_SET` | `r1,r5,r20,gap,vol` | ターミナル特徴量セット |
| `EML_ALPHA_MIN_FITNESS` | `-0.5` | 昇格最小フィットネス閾値 |
| `EML_ALPHA_MIN_RANK_IC` | `-0.5` | 昇格最小 rank IC 閾値 |
| `EML_BACKTEST_MODE` | `expanding` | バックテストモード |
| `EML_BACKTEST_MIN_TRAIN_DAYS` | `750` | 最小学習期間 (日) |
| `EML_BACKTEST_STEP_DAYS` | `5` | ウォークフォワードステップ (日) |
| `EML_BACKTEST_COST_BPS` | `2.0` | コスト (bps) |
| `EML_BACKTEST_SLIPPAGE_BPS` | `2.0` | スリッページ (bps) |

## 環境変数一覧 (FROST)

| 変数名 | デフォルト | 説明 |
|--------|-----------|------|
| `FROST_ENABLED` | `1` | `0` で FROST 無効化 |
| `FROST_DRY_RUN` | `0` | `1` で dry-run モード |
| `FROST_BATCH_LABEL` | `frost_v1` | バッチラベル |
| `FROST_TOP_K` | `25` | 最大選抜候補数 |
| `FROST_PROMOTION_TOP_K` | `5` | 最大昇格候補数 |
| `FROST_PBO_THRESHOLD` | `0.20` | PBO Hard Gate 上限 |
| `FROST_MIN_OOS_SHARPE` | `0.50` | OOS Sharpe Hard Gate 下限 |
| `FROST_MIN_RANK_IC` | `0.02` | Rank IC Hard Gate 下限 |
| `FROST_MAX_TURNOVER` | `4.0` | Turnover Hard Gate 上限 |
| `FROST_MAX_DRAWDOWN` | `0.20` | Max Drawdown Hard Gate 上限 |
| `FROST_MIN_REGIME_PASS_RATIO` | `0.75` | Regime Pass Hard Gate 下限 |
| `FROST_MAX_COMPLEXITY_SCORE` | `0.60` | Complexity Hard Gate 上限 |
| `FROST_MIN_SELECTION_STABILITY` | `0.60` | Selection Stability Hard Gate 下限 |
| `FROST_W_PREDICTIVE` | `0.20` | 予測力スコア重み |
| `FROST_W_OOS_SHARPE` | `0.15` | OOS Sharpe スコア重み |
| `FROST_W_REGIME_STABILITY` | `0.15` | レジーム安定性重み |
| `FROST_W_SELECTION_CONSISTENCY` | `0.10` | 選択一貫性重み |
| `FROST_W_CAPACITY` | `0.10` | 収益容量重み |
| `FROST_W_PBO_PENALTY` | `0.02` | PBO ペナルティ重み |
| `FROST_W_TURNOVER_PENALTY` | `0.10` | Turnover ペナルティ重み |
| `FROST_W_COMPLEXITY_PENALTY` | `0.05` | 複雑度ペナルティ重み |
| `FROST_W_DRAWDOWN_PENALTY` | `0.05` | DD ペナルティ重み |
| `FROST_W_FRAGILITY_PENALTY` | `0.03` | 脆弱性ペナルティ重み |

---

## 主要な設計方針・ガード

1. **NaN/Inf ガード**: `EMLNode.to_json()` で `raw_weight` の NaN/Inf を `null` に変換。FROST 全 writer でも `_safe_float()` / `_safe_json()` を必須適用
2. **UPSERT 安全性**: 全テーブルで `ON CONFLICT DO UPDATE` — 同一 `run_id` / `candidate_id` の再実行が安全
3. **trace_id 伝播**: run → candidates → backtest → promotion → audit_events → FROST 全層まで一貫
4. **dry_run 分離**: `dry_run=True` では canonical bridge への書き込みを行わない。`audit_events` / `frost_audit_event_bridges` には `DRY_RUN` 決定を記録
5. **安全式検証**: `assert_safe_expr()` でルックアヘッドバイアス含む危険な式を除外
6. **psycopg3 準拠**: `%s` プレースホルダー、`executemany`、明示的 `conn.commit()`
7. **FROST Hard Gates**: PBO > 0.20 / rank_ic < 0.02 / oos_sharpe < 0.50 / turnover > 4.0 / drawdown > 0.20 / regime_pass_ratio < 0.75 / complexity > 0.60 / selection_stability < 0.60 の 8 ゲートでスコア計算前排除
8. **PBO (CPCV 近似)**: Combinatorial Cross-Validation 近似でバックテスト過学習確率を推定。numpy 非依存の純 Python 実装
9. **Near-Duplicate 抑制**: formula token の Jaccard 類似度 ≥ 0.95 で重複候補を自動排除

---

## ディレクトリ構成

```
prostock/
├── analytics/python/
│   ├── alpha/
│   │   ├── eml/              # EML コアレイヤー (8モジュール)
│   │   └── promotion_bridge.py
│   ├── backtest/             # バックテストハーネス (6モジュール)
│   ├── features/             # 特徴量構築 (4モジュール)
│   ├── frost/                # FROST Meta-Fitness Engine (11モジュール) ← NEW
│   │   ├── frost_config.py        # FrostConfig dataclass + load_frost_config()
│   │   ├── frost_contracts.py     # 全 dataclass 定義
│   │   ├── frost_features.py      # 特徴量抽出関数群
│   │   ├── frost_metrics.py       # スコア計算・ペナルティ計算
│   │   ├── frost_stability.py     # fold/regime/sign 安定性スコア
│   │   ├── frost_pbo.py           # CPCV 近似 PBO・fragility
│   │   ├── frost_selector.py      # hard gate 判定・evaluate_candidate
│   │   ├── frost_ranker.py        # ランキング・near-dup 検出
│   │   ├── frost_decision_engine.py  # 最終ポリシー適用
│   │   ├── frost_runner.py        # E2E オーケストレーター
│   │   ├── frost_report_builder.py   # Markdown/JSON レポート生成
│   │   └── __init__.py
│   ├── io/                   # PostgreSQL IO層 (6モジュール)
│   │   ├── postgres_eml_alpha_writer.py
│   │   ├── postgres_eml_backtest_writer.py
│   │   ├── postgres_eml_evaluation_writer.py
│   │   ├── postgres_frost_writer.py          # FROST UPSERT ← NEW
│   │   ├── postgres_frost_audit_bridge.py    # audit_events 発行 ← NEW
│   │   └── postgres_frost_promotion_bridge.py  # 昇格 bridge ← NEW
│   ├── metrics/              # 評価指標 (5モジュール)
│   └── pg_io/                # Event Study IO層
├── docs/runbooks/
│   ├── event_study_pipeline_runbook.md
│   ├── frost_engine.md           # FROST Operations Runbook ← NEW
│   ├── frost_scorecard.md        # スコアカード詳細仕様 ← NEW
│   ├── frost_promotion_policy.md # 昇格ポリシー ← NEW
│   └── frost_failure_modes.md    # 障害モードと対処 ← NEW
├── qedschema/
│   ├── migrations/
│   │   ├── 060_frost_runs.sql
│   │   ├── 061_frost_fitness_candidates.sql
│   │   ├── 062_frost_evaluations.sql
│   │   ├── 063_frost_selection_decisions.sql
│   │   ├── 064_frost_promotion_bridges.sql
│   │   └── 065_frost_audit_event_bridges.sql
│   └── views/
│       ├── 060_v_frost_runs.sql
│       ├── 061_v_frost_candidate_scores.sql
│       ├── 062_v_frost_selection_summary.sql
│       └── 063_v_frost_promotion_status.sql
├── scripts/
│   ├── postgres/
│   │   └── run_eml_pipeline.py   # EML マスタースクリプト
│   └── frost/                    # FROST スクリプト群 ← NEW
│       ├── init_frost_tables.sh
│       ├── run_frost_engine.sh
│       ├── run_frost_backfill.sh
│       ├── run_frost_promote.sh
│       └── verify_frost_engine.sh
├── tests/
│   ├── unit/
│   │   ├── test_eml_layers.py    # EML ユニット (65 passed)
│   │   ├── test_frost_layers.py  # FROST ユニット (56 passed) ← NEW
│   │   └── ...
│   └── integration/
│       ├── test_eml_pipeline_integration.py  # EML 統合 (25 passed)
│       ├── test_frost_integration.py         # FROST 統合 (24 passed) ← NEW
│       └── ...
├── Makefile
└── README.md
```

---

## FROST Meta-Fitness Engine (Phase 5)

### 概要

FROST (Fitness-Ranked Overfitting Suppression Testing) は EML / event study / Scrapling 由来の alpha 候補を **10 軸評価** して SELECTED / HOLD / REJECTED / REVIEW_REQUIRED を決定する Phase 5 選抜層です。

### frost_score 計算式

```
frost_score =
  + w_predictive       × normalized_predictive_score    (0.20)
  + w_oos_sharpe       × normalized_oos_sharpe           (0.15)
  + w_regime_stability × normalized_regime_stability     (0.15)
  + w_selection_consistency × normalized_consistency    (0.10)
  + w_capacity         × normalized_capacity_score       (0.10)
  − w_pbo_penalty      × pbo_score                       (0.02)
  − w_turnover_penalty × turnover_penalty                (0.10)
  − w_complexity_penalty × complexity_penalty            (0.05)
  − w_drawdown_penalty × drawdown_penalty                (0.05)
  − w_fragility_penalty × fragility_penalty              (0.03)
```

### Hard Gates (いずれか 1 つでも違反 → REJECTED)

| ゲート | 条件 | デフォルト閾値 |
|--------|------|---------------|
| PBO 過学習 | `pbo_score >` | 0.20 |
| Rank IC 不足 | `rank_ic <` | 0.02 |
| OOS Sharpe 不足 | `oos_sharpe <` | 0.50 |
| Turnover 超過 | `turnover >` | 4.0 |
| Max Drawdown 超過 | `max_drawdown >` | 0.20 |
| Regime Pass 不足 | `regime_pass_ratio <` | 0.75 |
| 複雑度超過 | `complexity_score >` | 0.60 |
| 安定性不足 | `selection_consistency_score <` | 0.60 |

### FROST パイプライン実行

```bash
# 環境変数設定
export QED_PG_DSN="postgresql://postgres:postgres@localhost:5432/qed_dev"
export FROST_ENABLED=1
export FROST_DRY_RUN=0

# 初期セットアップ (migrations + views)
make frost-init

# FROST 選抜パイプライン実行
make frost-pipeline

# dry-run で確認
make frost-pipeline-dry

# 昇格実行
make frost-promote

# 全体検証
make frost-verify

# ステータス確認
make frost-status
```

### FROST DB テーブル

| テーブル | 主キー / UNIQUE | 概要 |
|----------|----------------|------|
| `frost_runs` | `run_id` | FROST 実行メタデータ |
| `frost_fitness_candidates` | `(run_id, candidate_hash)` | 評価対象候補 |
| `frost_evaluations` | `(run_id, candidate_id)` | 10 軸評価結果・frost_score |
| `frost_selection_decisions` | `(run_id, candidate_id)` | SELECTED/HOLD/REJECTED/REVIEW_REQUIRED |
| `frost_promotion_bridges` | `(run_id, candidate_id)` | 昇格 bridge |
| `frost_audit_event_bridges` | `bridge_id` | 4-status audit |

### FROST DB ビュー

| ビュー | 概要 |
|--------|------|
| `v_frost_runs` | 実行ダッシュボード (選抜率・平均スコア) |
| `v_frost_candidate_scores` | 候補ごとの全スコア一覧 |
| `v_frost_selection_summary` | 実行ごとの決定集計 |
| `v_frost_promotion_status` | 昇格候補の最新状態追跡 |

### 詳細ドキュメント

- [frost_engine.md](docs/runbooks/frost_engine.md) — Operations Runbook
- [frost_scorecard.md](docs/runbooks/frost_scorecard.md) — スコアカード詳細仕様
- [frost_promotion_policy.md](docs/runbooks/frost_promotion_policy.md) — 昇格ポリシー
- [frost_failure_modes.md](docs/runbooks/frost_failure_modes.md) — 障害モードと対処

---

## FROST v2 改善版レイヤー (Q.E.D. Enhancement Handoff)

> commit `940dca1` — 2025-06-12

### 概要

既存の FROST Meta-Fitness Engine (Phase 5) を壊さず、以下 9 レイヤーを追加実装。
全モジュール **pure Python / numpy 不使用** / **trace_id end-to-end** / **rerun-safe UPSERT** 準拠。

---

### P0-1: Formal Lag Analyzer

| ファイル | 概要 |
|----------|------|
| `analytics/python/alpha/eml/eml_lag_analyzer.py` | `LagAnnotation` / `LagSafetyProof` / `analyze_lag()` / `check_future_leakage()` |
| `analytics/python/alpha/eml/eml_ast_safety_proof.py` | AST 安全証明 DB 保存 (`ASTSafetyRecord`) |

---

### P0-2: OOS Signal Correlation Dedup

| ファイル | 概要 |
|----------|------|
| `analytics/python/frost/frost_signal_dedup.py` | `SignalDedupResult` / `apply_signal_dedup()` — OOS 相関閾値超過シグナルを重複排除 |

---

### P0-3: Regime Entropy

Shannon エントロピーでレジーム偏在度を定量化。均等分布 → スコア高、特定レジーム集中 → ペナルティ。

| ファイル | 概要 |
|----------|------|
| `analytics/python/metrics/regime_entropy.py` | `RegimeEntropyResult` / `build_regime_entropy_result()` / `regime_entropy_to_score_components()` |

**主要環境変数:** `FROST_REGIME_ENTROPY_ENABLED=1`, `FROST_REGIME_ENTROPY_MIN=0.60`

---

### P0-4: Fragility Surface Index (FSI)

パラメータ摂動 (`window` / `cutoff` / `threshold` / `vol_scale` 等) に対する局所安定性を曲面スコア化。コールバック型評価関数で backtest エンジンと疎結合。

| ファイル | 概要 |
|----------|------|
| `analytics/python/frost/frost_surface_sampler.py` | `ParameterSpec` / `PerturbationGrid` / `generate_surface_samples()` |
| `analytics/python/frost/frost_fragility_surface.py` | `FragilitySurfaceResult` / `compute_fragility_surface(eval_func)` / `fsi_hard_gate_pass()` |
| `qedschema/migrations/078_frost_fragility_surfaces.sql` | FSI 計算結果テーブル |

**主要環境変数:** `FROST_FSI_ENABLED=1`, `FROST_FSI_MAX=0.40`, `FROST_FSI_PENALTY_SCALE=0.25`

---

### P0-5: PostgreSQL Scale Strategy

| マイグレーション | 内容 |
|----------------|------|
| `079_frost_indexes.sql` | `frost_evaluations` / `selection_decisions` / `fitness_candidates` / `eml_backtest_folds` + JSONB GIN インデックス |
| `080_frost_materialized_views.sql` | `frost_candidate_summary_mv` / `frost_run_stats_mv` / `frost_decision_history_mv` |
| `081_frost_partitioning_prep.sql` | `frost_partition_registry` / `frost_table_size_log` / `frost_log_table_sizes()` 関数 / `frost_partition_migration_guide` view |

---

### P1-1: Alpha Genome Layer

候補式を 10 軸 (`momentum` / `mean_reversion` / `flow` / `volatility` / `value` / `event` / `macro` / `microstructure` / `sentiment` / `credit_leverage`) の因子 DNA ベクトルに分解。

| ファイル | 概要 |
|----------|------|
| `analytics/python/genome/alpha_genome_encoder.py` | `GenomeVector` / `encode_formula()` / `genome_cosine_similarity()` |
| `analytics/python/genome/alpha_genome_similarity.py` | `compute_novelty_scores()` / `find_genome_near_duplicates()` |
| `analytics/python/genome/alpha_genome_cluster.py` | K-Means++ pure Python / `ClusteringResult` |
| `analytics/python/genome/alpha_genome_report.py` | `GenomeReport` / `genome_report_to_frost_features()` |
| `analytics/python/genome/alpha_genome_runner.py` | `run_genome_layer()` — オーケストレーター |
| `qedschema/migrations/074〜076_*.sql` | genome_profiles / clusters / similarity_edges (上三角制約付き) |

---

### P1-2: Crowding Detector

候補シグナルを 15 既知因子 (`MOM_12M` / `BM_RATIO` / `LOW_VOL` 等) へ OLS 単回帰し、混雑度スコアを算出。

| ファイル | 概要 |
|----------|------|
| `analytics/python/frost/frost_known_factor_library.py` | `KnownFactor` × 15 / `match_formula_to_factors()` |
| `analytics/python/frost/frost_crowding.py` | `CrowdingScore` / `compute_crowding_score()` / pure Python OLS |
| `qedschema/migrations/077_frost_crowding_scores.sql` | Crowding スコアテーブル |

**主要環境変数:** `FROST_CROWDING_ENABLED=1`, `FROST_CROWDING_R2_MAX=0.80`

---

### P2-1: Causal Discovery Layer

先行/遅行相関の非対称性 (Granger 近似) と複数レジームでの係数不変性を検定。

| ファイル | 概要 |
|----------|------|
| `analytics/python/causal/causal_direction.py` | `CausalDirectionResult` / `compute_causal_direction()` |
| `analytics/python/causal/causal_invariance.py` | `InvarianceResult` / `compute_invariance()` |
| `analytics/python/causal/causal_diagnostics.py` | `CausalDiagnostics` / `causal_diagnostics_to_frost_features()` |
| `analytics/python/causal/causal_runner.py` | `run_causal_layer()` / `run_causal_batch()` |
| `analytics/python/causal/causal_bridge.py` | psycopg3 upsert ブリッジ |
| `qedschema/migrations/070〜073_*.sql` | runs / candidate_tests / invariance_results / promotion_gate |

---

### P2-2: PBO Parallelization

`multiprocessing` による CPCV 近似の並列化（シリアルフォールバック付き）。

| ファイル | 概要 |
|----------|------|
| `analytics/python/frost/frost_worker_pool.py` | `WorkerPoolConfig` / `parallel_map()` |
| `analytics/python/frost/frost_pbo_parallel.py` | `PBOTask` / `run_pbo_parallel()` / `build_pbo_tasks_from_evaluations()` |

**主要環境変数:** `FROST_PBO_PARALLEL_ENABLED=1`, `FROST_PBO_MAX_WORKERS=4`

---

### FROST v2 スコア式

```
frost_score_v2
  = a1*predictive + a2*oos_sharpe + a3*regime_stability
  + a4*selection_consistency + a5*capacity
  + a6*genome_novelty + a7*causal_validity + a8*regime_entropy
  - b1*pbo - b2*turnover - b3*complexity - b4*drawdown
  - b5v1*fragility
  - b5v2*fragility_surface - b6*crowding - b7*signal_duplication
```

| 軸 | デフォルト重み | v2 追加 |
|----|---------------|---------|
| `a6` genome_novelty | 0.05 | ✅ |
| `a7` causal_validity | 0.05 | ✅ |
| `a8` regime_entropy | 0.05 | ✅ |
| `b5v2` fragility_surface | 0.02 | ✅ |
| `b6` crowding | 0.05 | ✅ |
| `b7` signal_duplication | 0.03 | ✅ |

#### v2 Hard Gate 閾値 (FrostConfig デフォルト)

| Gate | 閾値 |
|------|------|
| `min_causal_direction_score` | ≥ 0.60 |
| `min_invariance_pass_ratio` | ≥ 0.70 |
| `min_genome_novelty_score` | ≥ 0.20 |
| `max_crowding_r2` | ≤ 0.80 |
| `max_fsi` | ≤ 0.40 |
| `min_regime_entropy` | ≥ 0.60 |
| `max_signal_corr` | ≤ 0.90 |

#### v2 切り替え

```python
from frost.frost_config import FrostConfig
from frost.frost_metrics import compute_scores_for_features_v2

cfg = FrostConfig(use_v2_score=True)
scores = compute_scores_for_features_v2(feat, config_dict=cfg.to_dict())
# → scores["frost_score_v2"] に v2 総合スコア
```

---

### v2 テスト

```bash
cd /home/user/prostock

# v2 レイヤー単体テスト (58 tests)
python3 -W ignore -m pytest tests/unit/test_frost_v2_layers.py -v

# 全スイート (312 passed, 24 skipped)
python3 -W ignore -m pytest tests/ -q --tb=no
```

---

### v2 改善版追加テーブル一覧

| マイグレーション番号 | テーブル / オブジェクト | レイヤー |
|---------------------|------------------------|---------|
| 070 | `causal_runs` | P2-1 |
| 071 | `causal_candidate_tests` | P2-1 |
| 072 | `causal_invariance_results` | P2-1 |
| 073 | `causal_promotion_gate` | P2-1 |
| 074 | `alpha_genome_profiles` | P1-1 |
| 075 | `alpha_genome_clusters` | P1-1 |
| 076 | `alpha_genome_similarity_edges` | P1-1 |
| 077 | `frost_crowding_scores` | P1-2 |
| 078 | `frost_fragility_surfaces` | P0-4 |
| 079 | 複合インデックス群 | P0-5 |
| 080 | Materialized Views × 3 | P0-5 |
| 081 | Partitioning 準備テーブル・関数 | P0-5 |
