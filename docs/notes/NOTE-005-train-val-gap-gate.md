# NOTE-005 — 追加ゲート候補 G5: train/val gap 直接ゲート

**登録日**: 2026-06-13  
**出典**: QED_REVIEW_2026-06-13.md §5「本レビュー独自の追加指摘」  
**種別**: 追加ゲート候補（憲法ギャップではないが、ADR-002 比較で凍結済みの観点）  
**優先度**: 中（P8 ablation 後に gate-0 評価）  
**ステータス**: Note登録済み・未実装  
**コード変更**: なし（Note登録のみ）

---

## 1. 問題の概要

PBO（過学習確率）は過学習の「推定量」であり、train/val gap は過学習の「直接観測量」である。  
**この 2 つは補完関係にあるが、FROST のゲートには gap の直接ゲートが存在しない**。

---

## 2. PBO と gap の違い

```
PBO  = 過学習している確率の推定値（間接的・確率論的）
        └── 計算が複雑、bootstrap が必要
        └── 「どのくらい過学習しているか」の直接数値ではない

gap  = train_metric - val_metric（直接観測・決定論的）
        └── 計算が単純（引き算 1 回）
        └── 「どのくらい過学習しているか」の直接数値
```

両者の役割:
- PBO が高い → 「この成績は偶然かもしれない」
- gap が大きい → 「train と val で実際に成績が乖離している」

PBO = 低いが gap = 大きい候補は、過学習しているにも関わらず PBO だけでは弾けない可能性がある。

---

## 3. 実装コンセプト

```python
# train/val gap ゲート — 疑似コード（実装時の参考）
MAX_TRAIN_VAL_GAP = 0.5  # SR ベースの場合の候補値（実測分布で調整）

def gate_train_val_gap(
    train_sharpe: float,
    val_sharpe: float,
    max_gap: float = MAX_TRAIN_VAL_GAP,
) -> GateResult:
    """
    train と val の Sharpe Ratio 差がしきい値を超えた場合にゲート失敗。
    gap > 0 が通常（train が val より良い）。gap < 0 は val が上回る稀なケース。
    """
    gap = train_sharpe - val_sharpe
    if gap > max_gap:
        return GateResult(
            passed=False,
            reason=f"train/val gap {gap:.3f} > 上限 {max_gap} (train={train_sharpe:.3f}, val={val_sharpe:.3f})"
        )
    return GateResult(passed=True)
```

---

## 4. 実装の簡易性

QED_REVIEW の表現: 「実装が一行に近いほど安い」

- コード量: 1 条件文（引き算 + 比較）
- 依存関係: `train_sharpe` と `val_sharpe` が FrostEvaluation に存在すれば即実装可能
- ADR-001 適合: 純 Python で完結（numpy 不要）

---

## 5. 閾値の検討

閾値は絶対値ではなく、既存候補の実測分布から決定することを推奨。

考慮事項:
- 指標を SR にするか IC にするか（FR ROST の主要指標に合わせる）
- 絶対差 vs 相対差（`gap / train_sharpe`）
- val 期間の長さによる調整（短い val 期間では gap がブレやすい）

最終閾値は P8 ablation で既存候補の分布確認後に決定する。

---

## 6. 実装配置（リファクタリング計画上の位置）

QED_REVIEW 判定: Note 登録 → **P8 ablation 後に gate-0 を通して採否決定**  
配置候補: FROST ハードゲート追加（または `frost_pbo.py` に gap 計算を追加）

gate-0 条件:
1. P8 ablation で既存ゲートの寄与を計測
2. gap ゲートの増分価値（PBO と非重複の候補を弾けるか）を測定
3. 増分価値確認後に FROST ハードゲートに追加

---

## 7. 依存関係

```
NOTE-005 (train/val gap ゲート)
    ← P8 軸 ablation（gate-0 評価が先）
    ← FrostEvaluation に train_sharpe / val_sharpe フィールドが必要
    ※ ADR-002 非依存
    ※ PBO（frost_pbo.py）と補完関係（両方あれば過学習検出が多層化）
    ※ NOTE-001 (DSR) とも補完（三層の過学習対策: DSR / PBO / gap）
```

---

## 8. 参照

- QED_REVIEW_2026-06-13.md §5 独自追加指摘（第二）
- ADR-002 AlphaEvo 比較（この観点は凍結済みとして登録）
- `analytics/python/frost/frost_pbo.py` — 現行の PBO 計算（相補関係）
- NOTE-001 (DSR) — 三層の過学習対策における第一層
