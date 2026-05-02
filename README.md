# prostock — EML Alpha Discovery Pipeline

**プロジェクト**: Q.E.D. EML (Evolved Machine Learning) Alpha Discovery & Backtest Pipeline  
**ステータス**: ✅ 実装完了・全テスト通過 (198 passed)  
**最終更新**: 2026-05-02

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

### ユニットテスト (173 passed)

| ファイル | テスト数 | カバレッジ |
|----------|----------|-----------|
| `test_eml_layers.py` | 65 | EML全レイヤー (Core/Tree/Metrics/Backtest/Search/IO/Master) |
| `test_eml_core.py` | 12 | EMLNode, depth, compiler |
| `test_audit_payload.py` | 14 | audit_events ペイロード検証 |
| `test_event_study_writer.py` | 12 | EventStudyWriter UPSERT |
| `test_target_rule_resolver.py` | 17 | TargetRuleResolver |
| `test_trace_id_consistency.py` | 13 | trace_id 伝播・一貫性 |

### 統合テスト (25 passed)

| ファイル | テスト数 | カバレッジ |
|----------|----------|-----------|
| `test_eml_pipeline_integration.py` | 25 | Phase A→E E2E、DB UPSERT安全性、NaN/Infガード、dry_run動作 |
| `test_pipeline_integration.py` | — | Event Study Pipeline Phase A→D |

**合計: 198 passed**

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

---

## 環境変数一覧

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

---

## 主要な設計方針・ガード

1. **NaN/Inf ガード**: `EMLNode.to_json()` で `raw_weight` の NaN/Inf を `null` に変換
2. **UPSERT 安全性**: 全テーブルで `ON CONFLICT DO UPDATE` — 同一 `run_id` / `candidate_id` の再実行が安全
3. **trace_id 伝播**: run → candidates → backtest → promotion → audit_events まで一貫
4. **dry_run 分離**: `dry_run=True` では `eml_alpha_promotion_bridge` / `knowledge_artifacts` に書き込まない。`audit_events` には `DRY_RUN` 決定を記録
5. **安全式検証**: `assert_safe_expr()` でルックアヘッドバイアス含む危険な式を除外
6. **psycopg3 準拠**: `%s` プレースホルダー、`executemany`、明示的 `conn.commit()`

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
│   ├── io/                   # PostgreSQL IO層 (3モジュール)
│   ├── metrics/              # 評価指標 (5モジュール)
│   └── pg_io/                # Event Study IO層
├── scripts/postgres/
│   └── run_eml_pipeline.py   # マスタースクリプト
├── tests/
│   ├── unit/                 # ユニットテスト (173 passed)
│   └── integration/          # 統合テスト (25 passed)
├── Makefile
└── README.md
```
