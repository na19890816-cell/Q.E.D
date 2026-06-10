# FROST Meta-Fitness Engine — Operations Runbook

**Version**: 1.0.0  
**Last Updated**: 2026-06-10  
**Owner**: ProStock Quant Infrastructure  
**System**: Phase 5 — Research / Selection Layer

---

## 1. 概要

FROST (Fitness-Ranked Overfitting Suppression Testing) Meta-Fitness Engine は、EML Alpha Discovery・event study・Scrapling 正規化信号・既存 alpha engine から生成された alpha 候補を 10 軸評価し、SELECTED / HOLD / REJECTED / REVIEW_REQUIRED を決定する Phase 5 選抜層です。

### アーキテクチャポジション

```
EML Alpha Discovery
    ↓
Event Study Bootstrap
    ↓
frost_fitness_candidates  ← FROST 入力
    ↓
FROST Meta-Fitness Engine  ← 本 Runbook の対象
    ↓
frost_selection_decisions
    ↓
frost_promotion_bridges → Q.E.D. Promotion Chain
    ↓
audit_events (4-status compliance)
```

---

## 2. 前提条件

### 2.1 PostgreSQL スキーマ

以下の migration が適用済みであること:

| ファイル | テーブル |
|---|---|
| `060_frost_runs.sql` | `frost_runs` |
| `061_frost_fitness_candidates.sql` | `frost_fitness_candidates` |
| `062_frost_evaluations.sql` | `frost_evaluations` |
| `063_frost_selection_decisions.sql` | `frost_selection_decisions` |
| `064_frost_promotion_bridges.sql` | `frost_promotion_bridges` |
| `065_frost_audit_event_bridges.sql` | `frost_audit_event_bridges` |

以下の view が適用済みであること:

| ファイル | ビュー名 |
|---|---|
| `060_v_frost_runs.sql` | `v_frost_runs` |
| `061_v_frost_candidate_scores.sql` | `v_frost_candidate_scores` |
| `062_v_frost_selection_summary.sql` | `v_frost_selection_summary` |
| `063_v_frost_promotion_status.sql` | `v_frost_promotion_status` |

確認コマンド:

```bash
make frost-verify
```

### 2.2 Python 環境

```bash
python3 --version   # 3.10 以上
pip show psycopg    # psycopg3 (not psycopg2)
```

### 2.3 環境変数

最低限必要な環境変数:

```bash
export QED_PG_DSN="postgresql://user:pass@host:5432/dbname"
export FROST_ENABLED=1
export FROST_DRY_RUN=0   # 本番実行時は 0、テスト時は 1
```

---

## 3. 基本操作

### 3.1 初期セットアップ

```bash
# migrations + views を一括適用
make frost-init

# dry-run で適用確認のみ
make frost-init-dry
```

### 3.2 FROST パイプライン実行

```bash
# 標準実行
make frost-pipeline

# dry-run モード (DB 書き込みなし)
make frost-pipeline-dry

# スクリプト直接実行 (オプション指定)
./scripts/frost/run_frost_engine.sh \
  --run-id "$(uuidgen)" \
  --batch-label "frost_v1_20260610" \
  --trace-id "$(uuidgen)" \
  --dry-run false
```

### 3.3 EML バックフィル実行

過去の EML 候補を遡及評価する場合:

```bash
make frost-backfill

# dry-run
make frost-backfill-dry
```

### 3.4 昇格実行

review_status='approved' の候補を Q.E.D. へ昇格:

```bash
make frost-promote

# dry-run
make frost-promote-dry
```

### 3.5 状態確認

```bash
# 実行サマリー
make frost-status

# 完全検証 (テーブル存在・件数・品質チェック)
make frost-verify
```

---

## 4. FROST パイプライン詳細

### 4.1 処理フロー

```
1. frost_fitness_candidates から候補取得
   ↓
2. frost_features.py: 特徴量抽出
   ↓
3. frost_metrics.py: スコア計算
4. frost_stability.py: 安定性評価
5. frost_pbo.py: PBO 推定
   ↓
6. frost_selector.py: hard gate 判定 + frost_score 算出
   ↓
7. frost_ranker.py: ランキング + near-duplicate 抑制
   ↓
8. frost_decision_engine.py: 最終ポリシー適用
   ↓
9. postgres_frost_writer.py: DB 書き込み (UPSERT)
10. postgres_frost_audit_bridge.py: audit_events 発行
11. postgres_frost_promotion_bridge.py: 昇格 bridge 書き込み
```

### 4.2 frost_score 計算式

```
frost_score = 
  + w_predictive       * normalized_predictive_score
  + w_oos_sharpe       * normalized_oos_sharpe
  + w_regime_stability * normalized_regime_stability
  + w_selection_consistency * normalized_selection_consistency
  + w_capacity         * normalized_capacity_score
  - w_pbo_penalty      * pbo_score
  - w_turnover_penalty * turnover_penalty
  - w_complexity_penalty * complexity_penalty
  - w_drawdown_penalty * drawdown_penalty
  - w_fragility_penalty * fragility_penalty
```

デフォルト重み (FrostConfig):

| 変数 | デフォルト値 |
|---|---|
| `FROST_W_PREDICTIVE` | 0.20 |
| `FROST_W_OOS_SHARPE` | 0.15 |
| `FROST_W_REGIME_STABILITY` | 0.15 |
| `FROST_W_SELECTION_CONSISTENCY` | 0.10 |
| `FROST_W_CAPACITY` | 0.10 |
| `FROST_W_PBO_PENALTY` | 0.02 |
| `FROST_W_TURNOVER_PENALTY` | 0.10 |
| `FROST_W_COMPLEXITY_PENALTY` | 0.05 |
| `FROST_W_DRAWDOWN_PENALTY` | 0.05 |
| `FROST_W_FRAGILITY_PENALTY` | 0.03 |

### 4.3 Hard Gates

以下いずれかに該当する候補は **frost_score によらず REJECTED**:

| ゲート | 条件 | デフォルト閾値 |
|---|---|---|
| PBO 過学習 | `pbo_score > threshold` | 0.20 |
| Rank IC 不足 | `rank_ic < min_rank_ic` | 0.02 |
| OOS Sharpe 不足 | `oos_sharpe < min_oos_sharpe` | 0.50 |
| Turnover 超過 | `turnover > max_turnover` | 4.0 |
| Max Drawdown 超過 | `max_drawdown > max_drawdown` | 0.20 |
| Regime Pass 不足 | `regime_pass_ratio < min_regime_pass_ratio` | 0.75 |
| 複雑度超過 | `complexity_score > max_complexity_score` | 0.60 |
| 安定性不足 | `selection_consistency_score < min_selection_stability` | 0.60 |

---

## 5. 監視・アラート

### 5.1 日常チェック

```sql
-- 直近 24h の実行状況
SELECT run_id, status, candidate_count, selected_count,
       started_at, ended_at
FROM v_frost_runs
WHERE started_at > now() - interval '24 hours'
ORDER BY started_at DESC;

-- 選抜サマリー
SELECT * FROM v_frost_selection_summary
WHERE created_at > now() - interval '7 days'
ORDER BY created_at DESC;
```

### 5.2 品質チェック

```sql
-- NULL frost_score の存在確認 (= 0 であるべき)
SELECT COUNT(*) FROM frost_evaluations WHERE frost_score IS NULL;

-- trace_id 欠落確認 (= 0 であるべき)
SELECT COUNT(*) FROM frost_runs WHERE trace_id IS NULL;

-- near-duplicate による REJECTED 確認
SELECT COUNT(*) FROM frost_selection_decisions
WHERE decision = 'REJECTED'
  AND decision_reason LIKE '%near_duplicate%';
```

### 5.3 昇格待ち確認

```sql
-- 承認待ち候補一覧
SELECT * FROM v_frost_promotion_status
WHERE promotion_status = 'pending';

-- REVIEW_REQUIRED で未承認
SELECT * FROM frost_selection_decisions
WHERE decision = 'REVIEW_REQUIRED'
  AND review_status != 'approved';
```

---

## 6. トラブルシューティング

### 6.1 frost_score が NULL になる

**原因**: `backtest_summary_json` や `metrics_json` が NULL または空  
**対処**:
```python
# frost_features.py の extract_all_features() を呼び出す前に確認
assert candidate.backtest_summary_json is not None
```

### 6.2 全候補が REJECTED になる

**原因**: Hard gate 閾値が厳しすぎる  
**対処**:
```bash
# 環境変数で閾値を緩める (開発・調査時のみ)
FROST_PBO_THRESHOLD=0.40 \
FROST_MIN_OOS_SHARPE=0.20 \
make frost-pipeline-dry
```

### 6.3 dry-run なのに DB に書き込まれる

**原因**: `FROST_DRY_RUN` 環境変数が未設定 (デフォルト `False`)  
**対処**:
```bash
export FROST_DRY_RUN=1
# または FrostConfig(dry_run=True) を明示的に渡す
```

### 6.4 UPSERT 時に unique constraint 違反

**原因**: `run_id` が重複している  
**対処**: 新規 `run_id = uuid.uuid4()` で再実行。UPSERT は ON CONFLICT DO UPDATE なので通常は発生しない。

### 6.5 psycopg3 接続エラー

```bash
# DSN フォーマット確認
psql "$QED_PG_DSN" -c "SELECT 1"

# psycopg3 インストール確認
python3 -c "import psycopg; print(psycopg.__version__)"
```

---

## 7. 設定一覧

### 7.1 全環境変数

```bash
# 基本設定
FROST_ENABLED=1              # 0 で無効化
FROST_DRY_RUN=0              # 1 で dry-run モード
FROST_BATCH_LABEL=frost_v1   # バッチラベル
FROST_ENGINE_VERSION=1.0.0

# 重み (合計 ≤ 1.0)
FROST_W_PREDICTIVE=0.20
FROST_W_OOS_SHARPE=0.15
FROST_W_REGIME_STABILITY=0.15
FROST_W_SELECTION_CONSISTENCY=0.10
FROST_W_CAPACITY=0.10
FROST_W_DIVERSIFICATION=0.05
FROST_W_PBO_PENALTY=0.02
FROST_W_TURNOVER_PENALTY=0.10
FROST_W_COMPLEXITY_PENALTY=0.05
FROST_W_DRAWDOWN_PENALTY=0.05
FROST_W_FRAGILITY_PENALTY=0.03

# 閾値
FROST_PBO_THRESHOLD=0.20
FROST_MIN_OOS_SHARPE=0.50
FROST_MIN_RANK_IC=0.02
FROST_MAX_TURNOVER=4.0
FROST_MAX_DRAWDOWN=0.20
FROST_MIN_REGIME_PASS_RATIO=0.75
FROST_MAX_COMPLEXITY_SCORE=0.60
FROST_MIN_SELECTION_STABILITY=0.60

# 選抜制限
FROST_TOP_K=25
FROST_PROMOTION_TOP_K=5
FROST_REQUIRE_AUDIT_PASS=1

# DB 設定
QED_PG_DSN=postgresql://user:pass@host:5432/dbname
```

---

## 8. Makefile ターゲット一覧

| ターゲット | 説明 |
|---|---|
| `make frost-init` | migrations + views 適用 |
| `make frost-init-dry` | 適用 dry-run |
| `make frost-pipeline` | FROST パイプライン実行 |
| `make frost-pipeline-dry` | パイプライン dry-run |
| `make frost-backfill` | EML 遡及評価 |
| `make frost-backfill-dry` | 遡及評価 dry-run |
| `make frost-promote` | 承認済み候補を昇格 |
| `make frost-promote-dry` | 昇格 dry-run |
| `make frost-verify` | 全体検証 |
| `make frost-status` | 実行ステータス確認 |
| `make frost-clean` | ローカルキャッシュ削除 |

---

## 9. 関連ドキュメント

- [frost_scorecard.md](frost_scorecard.md) — スコアカード詳細仕様
- [frost_promotion_policy.md](frost_promotion_policy.md) — 昇格ポリシー
- [frost_failure_modes.md](frost_failure_modes.md) — 障害モードと対処
- [event_study_pipeline_runbook.md](event_study_pipeline_runbook.md) — Event Study Runbook
