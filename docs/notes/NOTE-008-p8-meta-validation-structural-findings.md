# NOTE-008 — P8 メタ検証で判明した選抜器の構造的所見

**登録日**: 2026-10-03
**出典**: P8 メタ検証 (`analytics/python/frost/frost_meta_sensitivity.py`) の初回実行。
レポートは `docs/reports/frost_meta_validation_synthetic_2026-10-03.md`。
**種別**: 選抜器設計の課題（A1 / A3 の検証結果）
**優先度**: 中
**ステータス**: Note登録済み・未着手
**コード変更**: なし。P8 は観測のみ。ポリシー変更はこの Note を通じて通常の検証サイクルに回す。

---

## 0. 前提

本 Note の対象は、コードを読めば導けて、**データに依存しない**所見だけである。
合成データの数値（反転率など）は本番の性質を表さない。
数値は golden dataset を抽出したあとに再測定する（§4）。

## 1. 所見 A — `w_regime_stability` は Gate 通過候補の間で順位に効かない

- `regime_pass_ratio_raw` は 3 レジーム（bull/bear/crisis）中で Sharpe > 0 のレジームの割合なので、{0, 1/3, 2/3, 1} の 4 値しか取らない。
- Hard Gate `min_regime_pass_ratio = 0.75` を通過できるのは **ratio = 1.0 の候補だけ**。
- `compute_regime_stability_score = min(1, ratio + crisis_bonus)` で、ratio = 1.0 なら **常に 1.0**。
- したがって、Gate 通過集合では regime_stability 軸が定数になる。
  - 重み 0.15（v1 正方向重み 0.70 の 21%）は、全員のスコアに一律 +0.15 を足すだけ。
  - 選抜にも順位にも寄与しない。
- 実測（合成データ、seed 7/11/23）では、ablation 後の Kendall τ（Gate 通過のみ）がいずれも **1.000** だった。
- 閾値の掃引でも、0.67〜1.0 の範囲は通過集合が同一になる（離散 4 値のため）。
  - したがって、閾値感度で ±10/20% 動かしても regime_pass_ratio の反転率がほぼ 0 なのは、頑健だからではない。**離散化による見かけの安定**である。

**含意**: 軸は実質「ゲートの二重計上」になっている。主な選択肢は次の 3 つ。

1. 重みを他軸に再配分する
2. スコア側をレジームの Sharpe の連続値（最小レジーム Sharpe など）に置き換える
3. ゲートを緩めてスコアで連続的に評価する

どれを選ぶかは gate-0 評価で決める。

## 2. 所見 B — top_k が拘束しないと、重みは SELECTED 集合に一切効かない

- `select_diverse_top_k` は、Gate 通過候補をスコア順に top_k 件まで採る。
  - Gate 通過数 ≤ top_k（既定 25）のときは、**SELECTED 集合は Gate だけで決まる**。
  - このとき 10 軸の重みは、`promotion_top_k`（既定 5）の選別と borderline の REVIEW_REQUIRED 判定にしか効かない。
- 合成データ（Gate 通過 19 < 25）では、全 10 軸の ablation で TOP_K Jaccard = 1.0 になった。
- メタ検証レポートは、この状態を `top_k_binding=false` として警告する。
  - 重みの評価には、昇格上位 Jaccard（promo_jaccard）と τ（Gate 通過のみ）を使う。

**含意**: 本番で Gate 通過数が常時 top_k 未満なら、A1（軸削除）の議論は「昇格上位 5 件の並び」に限定してよい。
本番での Gate 通過数の分布を確認すること（`frost_evaluations.hard_gate_passed` の run 別集計）。

## 3. 所見 C — 昇格上位の境界は僅差になりやすい（A3 関連）

- 合成データでは、重み ±20% の摂動で昇格上位 3 件の Jaccard が p50 = 0.5 だった（τ p50 = 0.97 で、全体の順位はほぼ不変）。
- 原因は、3 位と 4 位のスコア差が 0.0018 と、重み摂動によるスコア変動より小さいこと。
- 線形加重和の上位境界は、重みの選び方次第で入れ替わる。
  - パレートフロンティア表示や、境界付近の REVIEW_REQUIRED を広げる案は、本番データで p05 を見てから判断する。

## 4. 次のアクション

1. golden dataset を抽出する（`Makefile.golden`）。そのうえで次を実行し、数値を本番の性質で置き換える。
   ```
   python scripts/frost/run_frost_meta_validation.py --candidates-json <golden>.json --write-db
   ```
2. 所見 A の対応案（1〜3）を gate-0 評価にかける。
   - 現行の PolicySpec を変える場合は policy_hash が変わるので、golden baseline を再生成する。
3. NOTE-004 / NOTE-005（追加ゲート候補）の gate-0 評価には、同じ `threshold_sensitivity` を使う。
