# FROST Failure Modes — 障害モードと対処

**Version**: 1.0.0  
**Last Updated**: 2026-06-10  
**Owner**: ProStock Quant Infrastructure

---

## 1. 概要

FROST Meta-Fitness Engine で発生しうる障害モードと、その検出方法・対処法・予防策を記載します。

障害は以下の 4 カテゴリに分類します:

1. **データ品質障害** — 入力データの欠損・異常値・フォーマット不整合
2. **計算障害** — スコア計算エラー・数値不安定性
3. **DB 障害** — 書き込みエラー・UPSERT 失敗・接続エラー
4. **設計障害** — 過学習・near-duplicate 見落とし・レジーム依存

---

## 2. データ品質障害

### 2.1 backtest_summary_json が NULL または空

**症状**:
- `frost_score` が NULL
- evaluation エラーログに `KeyError: 'oos_sharpe'`

**検出**:
```sql
SELECT COUNT(*) FROM frost_fitness_candidates
WHERE backtest_summary_json IS NULL OR backtest_summary_json = '{}'::jsonb;
```

**対処**:
1. EML パイプラインの出力を確認 (`eml_fitness_evaluations.backtest_summary_json`)
2. 欠損候補を再評価してから FROST に投入
3. `frost_features.py::extract_backtest_features` のデフォルト値が適用されるため、0.0 スコアとなりゲートで排除される

**予防**:
- `frost_fitness_candidates` への INSERT 前に `backtest_summary_json IS NOT NULL` をバリデーション

---

### 2.2 NaN / Inf が入力に含まれる

**症状**:
- `frost_score = NaN` または `frost_score = Inf`
- PostgreSQL に `NaN` がそのまま保存される

**検出**:
```sql
SELECT COUNT(*) FROM frost_evaluations
WHERE frost_score = 'NaN'::float OR frost_score = 'Infinity'::float;
```

**対処**:
- `postgres_frost_writer.py` の `_safe_float()` が NaN/Inf を `None` (SQL NULL) に変換
- 変換後に NULL になった列は診断 JSON に記録

**予防**:
- 全 writer で `_safe_float()` / `_safe_float_opt()` を必ず使用
- `frost_metrics.py` の全スコア計算で `math.isnan()` / `math.isinf()` チェック

---

### 2.3 fold_results が不足 (< 3 folds)

**症状**:
- `pbo_score` が信頼できない (CPCV 計算不能)
- `selection_consistency_score` が低く出る

**検出**:
```sql
SELECT candidate_id,
       (metrics_json->>'n_folds')::int AS n_folds
FROM frost_evaluations
WHERE (metrics_json->>'n_folds')::int < 3;
```

**対処**:
- `frost_pbo.py::estimate_pbo_from_folds`: fold 数 < 2 で `0.0` を返す (保守的)
- `frost_stability.py::compute_fold_sharpe_stability`: fold 数 < 2 で `stability_score = 0.0`

**予防**:
- EML バックテストで最低 3-fold の walk-forward を必須設定

---

### 2.4 candidate_hash 衝突

**症状**:
- 異なる候補が同一 `candidate_hash` を持つ
- near-duplicate 抑制が誤発火

**検出**:
```sql
SELECT candidate_hash, COUNT(*) AS cnt
FROM frost_fitness_candidates
WHERE run_id = 'YOUR_RUN_ID'
GROUP BY candidate_hash
HAVING COUNT(*) > 1;
```

**対処**:
- `candidate_id` (UUID) はユニーク保証あり → candidate_id 軸でアクセス
- near-duplicate 検出は formula token の Jaccard 類似度 ≥ 0.95 で別途判定

**予防**:
- `candidate_hash` は `formula_text` + `feature_spec_json` の SHA-256 → 衝突確率は無視できるレベル

---

## 3. 計算障害

### 3.1 全候補が REJECTED になる

**症状**:
- `v_frost_selection_summary` で `selected_count = 0`, `rejected_count = N`

**検出**:
```sql
SELECT gate_failure_reasons, COUNT(*)
FROM frost_selection_decisions
WHERE run_id = 'YOUR_RUN_ID' AND decision = 'REJECTED'
GROUP BY gate_failure_reasons
ORDER BY COUNT(*) DESC;
```

**原因の特定**:
```bash
# gate 別集計
psql "$QED_PG_DSN" -c "
SELECT 
    unnest(string_to_array(gate_failure_reasons, ',')) AS gate,
    COUNT(*)
FROM frost_selection_decisions
WHERE decision = 'REJECTED'
GROUP BY gate
ORDER BY COUNT(*) DESC;
"
```

**対処**:
1. 閾値が候補群に対して厳しすぎる → dry-run で閾値を緩めて調査
2. バッチの母集団の品質が低い → EML パイプラインを確認

```bash
# 閾値調整での dry-run 実行例
FROST_PBO_THRESHOLD=0.40 \
FROST_MIN_OOS_SHARPE=0.30 \
FROST_MIN_RANK_IC=0.01 \
make frost-pipeline-dry
```

---

### 3.2 frost_score が全候補で 0.0 になる

**症状**:
- `frost_evaluations.frost_score` が全件 0.0

**原因**:
- `compute_frost_score` に渡された全スコアが 0.0
- 特徴量抽出が全てデフォルト値 (0.0) を返している

**検出**:
```sql
SELECT 
    AVG(predictive_score),
    AVG(oos_sharpe),
    AVG(rank_ic),
    AVG(frost_score)
FROM frost_evaluations
WHERE run_id = 'YOUR_RUN_ID';
```

**対処**:
```python
# 手動デバッグ
from analytics.python.frost.frost_features import extract_all_features
features = extract_all_features(candidate)
print(features)  # 各特徴量を確認
```

---

### 3.3 near-duplicate 抑制の過剰発火

**症状**:
- 明らかに異なる候補が `near_duplicate` として REJECTED される

**検出**:
```sql
SELECT candidate_id, decision_reason
FROM frost_selection_decisions
WHERE decision = 'REJECTED'
  AND decision_reason LIKE '%near_duplicate%';
```

**対処**:
```python
# Jaccard 類似度の閾値を緩める (デフォルト 0.95)
from analytics.python.frost.frost_ranker import detect_near_duplicates
dups = detect_near_duplicates(candidates, threshold=0.99)  # 閾値を上げる
```

**予防**:
- near-duplicate 検出は `formula_text` の token Jaccard で行うため、formula の表記揺れに注意

---

### 3.4 PBO が常に 0.0 になる

**症状**:
- 全候補の `pbo_score = 0.0`

**原因**:
- `fold_results` が空 (`[]`)
- `fold_sharpes` の長さが < 2

**検出**:
```python
from analytics.python.frost.frost_pbo import compute_pbo_all
result = compute_pbo_all({})  # 空入力
print(result)  # {'pbo': 0.0, 'fragility': 0.0, 'final_pbo': 0.0}
```

**対処**:
- EML 候補に `fold_results` を必ず含める
- 最低 `[{"fold": 1, "oos_sharpe": x, "ic": y}, ...]` 形式で渡す

---

## 4. DB 障害

### 4.1 PostgreSQL 接続エラー

**症状**:
```
psycopg.OperationalError: connection to server failed
```

**検出**:
```bash
psql "$QED_PG_DSN" -c "SELECT 1"
```

**対処**:
1. `QED_PG_DSN` 環境変数を確認
2. PostgreSQL サービス起動確認: `pg_isready -d "$QED_PG_DSN"`
3. ファイアウォール・セキュリティグループ確認

---

### 4.2 UPSERT で constraint エラー

**症状**:
```
psycopg.errors.UniqueViolation: duplicate key value violates unique constraint
```

**原因**:
- ON CONFLICT の対象 constraint が存在しない
- migration が未適用

**検出**:
```bash
make frost-verify
```

**対処**:
```bash
# migration 再適用
make frost-init
```

---

### 4.3 JSONB 型エラー

**症状**:
```
psycopg.errors.InvalidTextRepresentation: invalid input syntax for type json
```

**原因**:
- `metrics_json` 等に Python オブジェクトをそのまま渡している

**対処**:
- `postgres_frost_writer.py` の `_safe_json()` を使用:
```python
def _safe_json(v):
    if v is None:
        return None
    if isinstance(v, str):
        return v
    return json.dumps(v, default=str)
```

---

### 4.4 trace_id が NULL になる

**症状**:
- `v_frost_promotion_status` で `trace_id IS NULL`

**検出**:
```sql
SELECT COUNT(*) FROM frost_runs WHERE trace_id IS NULL;
SELECT COUNT(*) FROM frost_fitness_candidates WHERE trace_id IS NULL;
SELECT COUNT(*) FROM frost_evaluations WHERE trace_id IS NULL;
```

**対処**:
```python
# frost_runner.py で trace_id を必ず生成
import uuid
if trace_id is None:
    trace_id = str(uuid.uuid4())
```

**予防**:
- `run_frost_pipeline()` の冒頭で `trace_id = trace_id or str(uuid.uuid4())`
- 全 I/O 関数で trace_id を必須引数として渡す

---

## 5. 設計障害

### 5.1 IC は高いが turnover が爆発

**症状**:
- `rank_ic ≥ 0.08` だが `turnover > 10.0`
- コスト控除後 Sharpe が大幅低下

**検出**:
```sql
SELECT candidate_id, rank_ic, 
       (backtest_json->>'annual_turnover')::float AS turnover,
       frost_score
FROM frost_evaluations
WHERE run_id = 'YOUR_RUN_ID'
  AND (backtest_json->>'annual_turnover')::float > 4.0
ORDER BY rank_ic DESC;
```

**対処**:
- Hard Gate `FROST_MAX_TURNOVER=4.0` が排除するはず
- 排除されていなければ `backtest_summary_json` の `annual_turnover` が正しく設定されているか確認

---

### 5.2 OOS Sharpe が fold で不安定

**症状**:
- バックテスト全体では Sharpe ≥ 1.0 だが fold 間のブレが大きい
- `fold_sharpe_stability.stability_score < 0.3`

**対処**:
- `selection_consistency_score < 0.60` の Hard Gate で排除される
- 排除されない場合は `fragility_penalty` でスコアが低下

---

### 5.3 単一レジーム依存

**症状**:
- bull market では強いが bear / crisis で壊滅
- `regime_pass_ratio < 0.75` → Hard Gate REJECTED

**検出**:
```sql
SELECT candidate_id,
       (regime_json->>'bull_sharpe')::float AS bull,
       (regime_json->>'bear_sharpe')::float AS bear,
       (regime_json->>'crisis_sharpe')::float AS crisis,
       regime_stability_score
FROM frost_evaluations
WHERE run_id = 'YOUR_RUN_ID'
ORDER BY regime_stability_score ASC;
```

---

### 5.4 EML 式が複雑すぎる

**症状**:
- `complexity_score > 0.60` → Hard Gate REJECTED

**検出**:
```sql
SELECT candidate_id, formula_text, complexity_score
FROM frost_fitness_candidates
WHERE complexity_score > 0.60
ORDER BY complexity_score DESC;
```

**対処**:
- EML パイプラインの `complexity_penalty` 重みを上げる
- EML 側の formula 長さ制限を設ける

---

### 5.5 near-duplicate が多すぎる

**症状**:
- 同一家族の変種 formula が大量に FROST に投入される
- near-duplicate 抑制が多数発火し、選択多様性が低下

**検出**:
```sql
SELECT 
    COUNT(DISTINCT candidate_id) AS total_candidates,
    COUNT(CASE WHEN decision_reason LIKE '%near_duplicate%' THEN 1 END) AS dup_rejected
FROM frost_selection_decisions
WHERE run_id = 'YOUR_RUN_ID';
```

**対処**:
- EML 側でバッチ投入前に formula token 類似度フィルタリングを実施
- FROST の `detect_near_duplicates(threshold=0.95)` は最後の防衛線

---

### 5.6 Scrapling 由来信号の time alignment ミス

**症状**:
- 高い IC が報告されるが look-ahead bias が疑われる
- `source_system = 'scrapling'` の候補が不自然に高スコア

**対処**:
1. Scrapling 信号の `effective_date` を確認
2. バックテスト期間と信号の利用可能日を照合
3. no-lookahead checks を EML パイプラインで強化

**予防**:
- Scrapling 由来候補は `source_type = 'scrapling_normalized_signal'` でタグ付け
- レビュー時に `source_type` フィルタで別途確認

---

### 5.7 audit_events は通るが selection が brittle

**症状**:
- audit_events は APPLIED ステータスで記録
- しかし実際の performance が不安定

**原因**:
- FROST の評価は過去データのみ → 将来の OOS は保証しない

**対処**:
- FROST は選抜の gate であり、本番 performance 保証ではないことを明記
- 昇格後の本番 OOS モニタリングを Q.E.D. 側で別途実施

---

## 6. インシデント対応チェックリスト

障害発生時の確認順序:

```
□ 1. make frost-verify で全体ヘルスチェック
□ 2. psql "$QED_PG_DSN" -c "SELECT 1" で DB 接続確認
□ 3. frost_runs.status = 'error' のレコードを確認
□ 4. trace_id を使って関連 audit_events を追跡
□ 5. frost_evaluations の NULL frost_score 件数確認
□ 6. hard gate 別の REJECTED 集計確認
□ 7. dry-run で再実行して再現性を確認
□ 8. 必要に応じて閾値を緩めて問題の根本原因を特定
```

---

## 7. ログとトレース

### 7.1 trace_id による追跡

```sql
-- 1つの trace_id の全イベントを時系列で追跡
SELECT 
    ae.occurred_at,
    ae.event_name,
    ae.decision,
    ae.entity_type,
    ae.entity_id
FROM audit_events ae
WHERE ae.trace_id = 'YOUR_TRACE_ID'
ORDER BY ae.occurred_at ASC;
```

### 7.2 エラーレコードの確認

```sql
-- FROST 実行エラー
SELECT run_id, status, error_message, started_at
FROM frost_runs
WHERE status = 'error'
ORDER BY started_at DESC;

-- 昇格エラー
SELECT bridge_id, candidate_id, promotion_status, error_message
FROM frost_promotion_bridges
WHERE promotion_status = 'error'
ORDER BY created_at DESC;
```

---

## 8. 関連ドキュメント

- [frost_engine.md](frost_engine.md) — Engine Operations Runbook
- [frost_scorecard.md](frost_scorecard.md) — スコアカード詳細仕様
- [frost_promotion_policy.md](frost_promotion_policy.md) — 昇格ポリシー
