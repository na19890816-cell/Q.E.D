# NOTE-001 — 憲法ギャップ G1: Deflated Sharpe Ratio（DSR）未実装

**登録日**: 2026-06-13  
**出典**: QED_REVIEW_2026-06-13.md §3「憲法ギャップ監査 — ギャップ1」  
**種別**: 憲法ギャップ（外部提案ではなく、設計憲法が既に義務付けている未実装項目）  
**優先度**: 最優先（憲法違反）  
**ステータス**: ✅ 実装完了 (2026-09-30, Phase 4b) — N は ADR-002 整備まで暫定仮定値  
**コード変更**: `analytics/python/frost/frost_dsr.py` / `tests/unit/test_phase4_dsr.py`

---

## 1. ギャップの概要

設計憲法は「シグナル採用前の Deflated Sharpe Ratio（DSR）算出」を要求している。  
QED レビューで「改善提案」として挙げられたが、これは提案ではなく **憲法違反の指摘** として扱う。

**現状**: FROST のハードゲート 8+7 個のどこにも DSR が存在しない。

---

## 2. DSR とは

Deflated Sharpe Ratio は、バックテストの多重比較バイアス（over-fitting）を補正した Sharpe Ratio。  
通常の SR は「試したが捨てた候補」を無視して最良のものだけを評価するため、楽観バイアスを持つ。  
DSR はその探索コスト（試行回数 N）を分母に組み込み、真の期待超過収益を保守的に推定する。

```
DSR = SR × φ((SR - E[SR_max]) / √Var[SR_max])
```

- `SR`　　　: 候補の Sharpe Ratio（バックテスト）
- `N`　　　 : そのアルファに到達するまでに探索した候補総数
- `E[SR_max]`: N 個の独立 SR の期待最大値（理論計算）
- `Var[SR_max]`: 同分散

DSR < 0 であれば、探索コストを考慮した真の優位性はゼロ以下と判定される。

---

## 3. 実装上の前提条件

### 3.1 試行回数 N の集計

DSR の計算には「そのシグナルに到達するまでに何候補を探索したか」という run 横断の累積試行数 N が必要。

**現状の問題**: 現在のスキーマで run 横断の累積試行数を集計できるか未確認。

### 3.2 ADR-002 系譜ログとの依存関係

累積試行数の集計基盤として **ADR-002 の系譜ログ（B 設計）** が必要になる可能性が高い。  
つまり **DSR 実装は ADR-002 系譜ログ設計と接続しており、ADR-002 先行の根拠をさらに強化する**。

```
ADR-002 (系譜ログ B 設計)
    └── run 横断の累積試行数 N 集計
            └── DSR 算出（憲法ギャップ G1 解消）
```

---

## 4. 実装候補場所

| 候補 | 位置づけ |
|---|---|
| FROST ハードゲート追加 | DSR < 0 でゲート失敗 → `gate_pass = False` |
| FROST スコア軸追加 | DSR を 10 軸の 1 軸として soft スコアに組み込み |
| 昇格前ゲート追加 | `frost_promotion_policy` のレイヤーで昇格時に DSR チェック |

**推奨**: ハードゲート追加（DSR < 0 は昇格不可）。P8 ablation 結果を見てから最終決定。

---

## 5. 実装配置（リファクタリング計画上の位置）

QED_REVIEW トリアージ判定: **採用（最優先）**  
配置: **P8（軸 ablation フェーズ）** で試行統計集計と合わせて実装。  
前提: ADR-002 系譜ログ設計が先行すること。

---

## 6. 依存関係

```
NOTE-001 (DSR)
    ← ADR-002 系譜ログ（試行数 N の集計基盤）
    ← P8 軸 ablation（DSR 追加前に既存軸の寄与を測定）
```

---

## 7. 参照

- QED_REVIEW_2026-06-13.md §3 ギャップ1
- QED_REVIEW_2026-06-13.md §4 トリアージ #1
- ADR-002 系譜ログ（ドラフト）
- 設計憲法（シグナル採用前 DSR 算出義務）
- Bailey, D.H., López de Prado, M. (2014). "The Deflated Sharpe Ratio: Correcting for Selection Bias, Backtest Overfitting, and Non-Normality."

---

## 8. 実装記録 (Phase 4b, 2026-09-30)

### 8.1 実装内容

| 項目 | 内容 |
|---|---|
| モジュール | `analytics/python/frost/frost_dsr.py`（純 Python / numpy・statistics・scipy 不使用） |
| 公開 API | `norm_cdf` / `norm_ppf` / `sample_moments` / `probabilistic_sharpe_ratio` / `expected_max_sharpe` / `deflated_sharpe_ratio` / `DsrParams` / `DsrGate` / `check_dsr_gate` |
| 配置 | 昇格前スタンドアロンゲート（§4 推奨の「DSR < 閾値は昇格不可」）。gate_engine 統合は P8 ablation 後 |
| PolicySpec | `min_dsr=0.95` / `dsr_default_n_trials=1`（hard_gates セクション, policy_hash 対象） |
| テスト | `tests/unit/test_phase4_dsr.py`（マーカー `phase4_dsr`） |

### 8.2 数式の訂正

本 Note §2 および HANDOVER_2026-09-30 §5 に記載された式は原論文の定義と一致しないため、実装は原論文に従った:

```
DSR = PSR(SR0) = Φ( (SR^ − SR0)·√(T−1) / √(1 − γ3·SR^ + (γ4−1)/4·SR^²) )
SR0 = √V[SR] · ((1−γ)·Φ⁻¹(1−1/N) + γ·Φ⁻¹(1−1/(N·e)))      γ: Euler–Mascheroni
```

DSR は確率値 ∈ [0,1] であり、「DSR < 0」ではなく「DSR < min_dsr (0.95)」で不合格と判定する。

### 8.3 検証

- 論文数値例（年率 SR=2.5, T=1250, N=100, V=0.5, 歪度 −3, 尖度 10）→ DSR = 0.9004 / 年率 SR0 = 1.789（論文値と一致）
- `norm_ppf` は scipy 比 最大誤差 < 1e-8、`norm_cdf` は < 1e-15
- 歪度・尖度は scipy.stats と 1e-12 以内で一致

### 8.4 残課題（ADR-002 依存）

- 試行回数 N は現状 `dsr_default_n_trials`（=1, 退化版 DSR = PSR(0)）を仮定。仮定時は `review_required=True` を強制
- ADR-002 系譜ログで run 横断の累積試行数・兄弟候補 SR が集計できた時点で `n_trials` / `trial_sharpes` を供給する
