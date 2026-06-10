# FROST Scorecard — 評価軸詳細仕様

**Version**: 1.0.0  
**Last Updated**: 2026-06-10  
**Owner**: ProStock Quant Infrastructure

---

## 1. 概要

FROST Scorecard は、各 alpha 候補に対して 10 軸の評価を行い、最終的な `frost_score` を算出する仕様書です。  
各軸の計算方法・正規化手法・ペナルティ計算を定義します。

---

## 2. スコアカード全体像

```
frost_score (0〜1 範囲にクリップ)
├── Positive Components (加算)
│   ├── predictive_score     × w_predictive       (0.20)
│   ├── oos_sharpe_score     × w_oos_sharpe        (0.15)
│   ├── regime_stability_score × w_regime_stability (0.15)
│   ├── selection_consistency_score × w_selection_consistency (0.10)
│   └── capacity_score       × w_capacity          (0.10)
│
└── Penalty Components (減算)
    ├── pbo_score            × w_pbo_penalty       (0.02)
    ├── turnover_penalty     × w_turnover_penalty  (0.10)
    ├── complexity_penalty   × w_complexity_penalty (0.05)
    ├── drawdown_penalty     × w_drawdown_penalty  (0.05)
    └── fragility_penalty    × w_fragility_penalty (0.03)
```

---

## 3. 各スコア軸の仕様

### 3.1 Predictive Score (予測力スコア)

**モジュール**: `frost_metrics.py::compute_predictive_score`  
**入力**: `rank_ic`, `ic_t_stat`  
**出力範囲**: [0.0, 1.0]

**計算方法**:
```
rank_ic_score = clamp(rank_ic / 0.15, 0.0, 1.0)  # 0.15 以上で満点
t_stat_bonus  = 0.1 * clamp(ic_t_stat / 2.0, 0.0, 1.0)  # t > 2 でボーナス
predictive_score = min(rank_ic_score + t_stat_bonus, 1.0)
```

**解釈ガイド**:

| スコア範囲 | 解釈 |
|---|---|
| 0.80 〜 1.00 | 非常に高い予測力 (rank_ic ≥ 0.12) |
| 0.50 〜 0.80 | 良好な予測力 |
| 0.20 〜 0.50 | 中程度 |
| 0.00 〜 0.20 | Hard gate 通過ギリギリ (rank_ic ≥ 0.02) |

**Hard Gate**: `rank_ic < 0.02` → REJECTED (スコア計算前に排除)

---

### 3.2 OOS Sharpe Score (アウトオブサンプル Sharpe スコア)

**モジュール**: `frost_metrics.py::compute_oos_sharpe_score`  
**入力**: `oos_sharpe`  
**出力範囲**: [0.0, 1.0]

**計算方法**:
```
# 0.5 未満は Hard Gate (排除)
# 0.5〜3.0 を線形マッピング → [0.0, 1.0]
oos_sharpe_score = clamp((oos_sharpe - 0.5) / 2.5, 0.0, 1.0)
```

**解釈ガイド**:

| OOS Sharpe | スコア |
|---|---|
| ≥ 3.0 | 1.00 |
| 2.0 | 0.60 |
| 1.0 | 0.20 |
| < 0.5 | Hard Gate REJECTED |

---

### 3.3 Regime Stability Score (レジーム安定性スコア)

**モジュール**: `frost_stability.py::compute_all_stability` + `frost_metrics.py::compute_regime_stability_score`  
**入力**: `regime_breakdown_json` (bull/bear/crisis/neutral の Sharpe)  
**出力範囲**: [0.0, 1.0]

**計算方法**:
```
# 各レジームの Sharpe 符号チェック
positive_regimes = sum(1 for s in regime_sharpes if s > 0)
regime_pass_ratio = positive_regimes / len(regime_sharpes)

# 分散を安定性に変換
mean_sharpe = mean(regime_sharpes)
std_sharpe  = std(regime_sharpes)
cv = std_sharpe / (abs(mean_sharpe) + 1e-8)  # 変動係数
regime_stability_score = clamp(1.0 - cv / 2.0, 0.0, 1.0)
```

**Hard Gate**: `regime_pass_ratio < 0.75` → REJECTED

---

### 3.4 Selection Consistency Score (選択一貫性スコア)

**モジュール**: `frost_stability.py::compute_selection_consistency_score`  
**入力**: `fold_results` の Sharpe / IC 系列  
**出力範囲**: [0.0, 1.0]

**計算方法**:
```
fold_stability = compute_fold_sharpe_stability(fold_sharpes).stability_score
sign_stability = compute_sign_stability(fold_sharpes)
ic_stability   = compute_sign_stability(fold_ics)

selection_consistency_score = 
    0.50 * fold_stability + 
    0.30 * sign_stability + 
    0.20 * ic_stability
```

**Hard Gate**: `selection_consistency_score < 0.60` → REJECTED

---

### 3.5 Capacity Score (収益容量スコア)

**モジュール**: `frost_features.py::estimate_capacity_score`  
**入力**: `turnover`, `max_position_size`, `avg_holding_period_days`  
**出力範囲**: [0.0, 1.0]

**計算方法**:
```
# turnover が低いほど高 capacity (反転マッピング)
turnover_factor = clamp(1.0 - turnover / FROST_MAX_TURNOVER, 0.0, 1.0)
capacity_score  = turnover_factor  # 必要に応じて他指標と加重
```

---

### 3.6 PBO Penalty (過学習確率ペナルティ)

**モジュール**: `frost_pbo.py::estimate_pbo_from_folds`  
**アルゴリズム**: CPCV (Combinatorial Purged Cross-Validation) 近似  
**出力範囲**: [0.0, 1.0]

**計算方法**:
```
# Fold の OOS Sharpe と in-sample Sharpe を比較
# 最良 in-sample fold の OOS が最良になる確率を推定

combinations = {すべての (train, test) 分割}  # 最大 256 組
pbo = mean([1 if oos_rank(best_is_fold) > 0.5 else 0 for each combo])
```

**最終 PBO スコア**:
```
final_pbo = 0.7 * raw_pbo + 0.3 * fragility_score
```

**Hard Gate**: `pbo_score > 0.20` → REJECTED

---

### 3.7 Turnover Penalty (売買回転ペナルティ)

**モジュール**: `frost_metrics.py::compute_turnover_penalty`  
**入力**: `annual_turnover`  
**出力範囲**: [0.0, 1.0]

**計算方法**:
```
# 4.0 以下は 0、4.0〜8.0 で線形増加、8.0 以上で 1.0
if turnover <= 4.0:
    penalty = 0.0
elif turnover <= 8.0:
    penalty = (turnover - 4.0) / 4.0
else:
    penalty = 1.0
```

**Hard Gate**: `turnover > 4.0` → REJECTED

---

### 3.8 Complexity Penalty (複雑度ペナルティ)

**モジュール**: `frost_metrics.py::compute_complexity_penalty`  
**入力**: `complexity_score` (EML から継承)  
**出力範囲**: [0.0, 1.0]

**計算方法**:
```
complexity_penalty = clamp(complexity_score, 0.0, 1.0)
```

**Hard Gate**: `complexity_score > 0.60` → REJECTED

---

### 3.9 Drawdown Penalty (最大 DD ペナルティ)

**モジュール**: `frost_metrics.py::compute_drawdown_penalty`  
**入力**: `max_drawdown` (正値表現, 0.20 = 20% DD)  
**出力範囲**: [0.0, 1.0]

**計算方法**:
```
# 0.20 以下は 0、0.20〜0.60 で線形増加
if max_drawdown <= 0.20:
    penalty = 0.0
elif max_drawdown <= 0.60:
    penalty = (max_drawdown - 0.20) / 0.40
else:
    penalty = 1.0
```

**Hard Gate**: `max_drawdown > 0.20` → REJECTED

---

### 3.10 Fragility Penalty (脆弱性ペナルティ)

**モジュール**: `frost_pbo.py::compute_selection_fragility`  
**アルゴリズム**: Leave-One-Fold-Out (LOFO) 不安定性  
**出力範囲**: [0.0, 1.0]

**計算方法**:
```
# fold を 1 つ外したときの選択順位の変動
# Sharpe rank の LOFO 標準偏差を正規化
rank_changes = [rank_change when removing fold_i for i in 1..n]
fragility_score = clamp(std(rank_changes) / (n_folds + 1e-8), 0.0, 1.0)
```

---

## 4. ロバスト正規化

スコア計算では、外れ値の影響を受けにくいロバスト正規化を使用します。

**モジュール**: `frost_metrics.py::robust_normalize`

```python
def robust_normalize(values, clip_min=-3.0, clip_max=3.0):
    median = statistics.median(values)
    q75, q25 = percentile(values, 75), percentile(values, 25)
    iqr = q75 - q25
    
    if iqr < 1e-10:
        return [0.0] * len(values)
    
    # ロバスト z-score: (x - median) / (IQR / 1.35)
    sigma = iqr / 1.35
    normalized = [(v - median) / sigma for v in values]
    return [max(clip_min, min(clip_max, v)) for v in normalized]
```

**適用箇所**:
- batch 評価時の cross-candidate 正規化
- fold 間 Sharpe の比較

---

## 5. Hard Gate 一覧と優先順位

Hard Gate は **frost_score を計算する前に** 評価されます。  
いずれかに該当すると即 REJECTED (スコアは記録するが決定は REJECTED)。

| 優先順位 | ゲート名 | 条件 | 環境変数 |
|---|---|---|---|
| 1 | PBO 過学習 | `pbo_score > FROST_PBO_THRESHOLD` | `FROST_PBO_THRESHOLD=0.20` |
| 2 | Rank IC 不足 | `rank_ic < FROST_MIN_RANK_IC` | `FROST_MIN_RANK_IC=0.02` |
| 3 | OOS Sharpe 不足 | `oos_sharpe < FROST_MIN_OOS_SHARPE` | `FROST_MIN_OOS_SHARPE=0.50` |
| 4 | Turnover 超過 | `turnover > FROST_MAX_TURNOVER` | `FROST_MAX_TURNOVER=4.0` |
| 5 | Max DD 超過 | `max_drawdown > FROST_MAX_DRAWDOWN` | `FROST_MAX_DRAWDOWN=0.20` |
| 6 | Regime Pass 不足 | `regime_pass_ratio < FROST_MIN_REGIME_PASS_RATIO` | `FROST_MIN_REGIME_PASS_RATIO=0.75` |
| 7 | 複雑度超過 | `complexity_score > FROST_MAX_COMPLEXITY_SCORE` | `FROST_MAX_COMPLEXITY_SCORE=0.60` |
| 8 | 安定性不足 | `selection_consistency_score < FROST_MIN_SELECTION_STABILITY` | `FROST_MIN_SELECTION_STABILITY=0.60` |

---

## 6. 決定ロジック

### 6.1 決定フロー

```
1. Hard Gate 判定
   → 1つでも違反 → decision = REJECTED, gate_failures を記録

2. frost_score 計算
   → 加重線形結合 → [0, 1] にクリップ

3. ランキング
   → frost_score 降順でソート
   → near-duplicate 候補は上位のみ残す

4. 決定割り当て
   → rank ≤ FROST_TOP_K かつ gate_pass → SELECTED
   → rank > FROST_TOP_K かつ gate_pass → HOLD
   → gate_fail → REJECTED

5. 最終ポリシー適用
   → SELECTED 数が FROST_TOP_K を超えないよう制限
   → borderline (SELECTED スコアの下位 5%) → REVIEW_REQUIRED
   → FROST_PROMOTION_TOP_K 以内の SELECTED のみ promotion_eligible=True
```

### 6.2 決定コード一覧

| 決定コード | 意味 | promotion_eligible |
|---|---|---|
| `SELECTED` | 採択 | 上位 K 件のみ True |
| `HOLD` | 保留 (次回再評価) | False |
| `REJECTED` | 棄却 | False |
| `REVIEW_REQUIRED` | 要審査 (borderline) | False (承認後 True) |

---

## 7. スコアの DB 保存

`frost_evaluations` テーブルに全スコアが列として保存されます:

```sql
SELECT 
    candidate_id,
    predictive_score,
    oos_sharpe,
    rank_ic,
    regime_stability_score,
    selection_consistency_score,
    capacity_score,
    pbo_score,
    turnover_penalty,
    complexity_penalty,
    drawdown_penalty,
    fragility_penalty,
    frost_score,
    gate_pass,
    gate_failure_reasons
FROM frost_evaluations
WHERE run_id = 'YOUR_RUN_ID'
ORDER BY frost_score DESC NULLS LAST;
```

---

## 8. 関連ドキュメント

- [frost_engine.md](frost_engine.md) — Engine Operations Runbook
- [frost_promotion_policy.md](frost_promotion_policy.md) — 昇格ポリシー
- [frost_failure_modes.md](frost_failure_modes.md) — 障害モードと対処
