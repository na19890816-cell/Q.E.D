# NOTE-004 — 追加ゲート候補 G4: 最小シグナル数ゲート

**登録日**: 2026-06-13  
**出典**: QED_REVIEW_2026-06-13.md §5「本レビュー独自の追加指摘」  
**種別**: 追加ゲート候補（憲法ギャップではないが、ADR-002 比較で凍結済みの観点）  
**優先度**: 中（P8 ablation 後に gate-0 評価）  
**ステータス**: Note登録済み・未実装  
**コード変更**: なし（Note登録のみ）

---

## 1. 問題の概要

シグナル数が少ない候補は、偶然の好成績（Lucky SR）を出しやすい。  
**現在の FROST ゲート一覧に最小シグナル数の下限が存在しない**。

---

## 2. なぜこのシステムで特に効くか

BSSM（注258）の知見: 「上位 4% が全利益」  
→ 少数の当たりシグナルが統計量全体を支配する現象がこの宇宙で確認されている。

シグナル数 N が小さい場合:
- SR の標準誤差が大きく、ノイズとの区別が難しい
- PBO（過学習確率）の推定精度が低下する
- backtesting 期間の長さによっては、1〜2 回の大勝が SR を押し上げるだけで通過できる

DSR（NOTE-001）と相補的: DSR は「探索試行数に対する補正」、最小シグナル数は「統計量の信頼性の最低保証」。

---

## 3. 実装コンセプト

```python
# 最小シグナル数ゲート — 疑似コード（実装時の参考）
MIN_SIGNAL_COUNT = 252  # 1年分（営業日）を最低ラインとする候補値

def gate_min_signal_count(
    signal_count: int,
    min_count: int = MIN_SIGNAL_COUNT,
) -> GateResult:
    if signal_count < min_count:
        return GateResult(
            passed=False,
            reason=f"シグナル数 {signal_count} < 最小要件 {min_count}"
        )
    return GateResult(passed=True)
```

**閾値候補**:
| 閾値 | 根拠 |
|---|---|
| 63 取引日（3ヶ月） | 絶対最小（これ未満は統計的に無意味）|
| 126 取引日（6ヶ月） | 保守的最小ライン |
| 252 取引日（1年） | 推奨値（季節性1サイクルをカバー）|

最終閾値は P8 ablation で既存候補の分布を確認してから決定する。

---

## 4. 実装の簡易性

- コード量: ほぼ 1 条件文
- 依存関係: なし（既存の FrostEvaluation に `signal_count` フィールドがあれば即実装可能）
- ADR-001 適合: 純 Python で完結（numpy 不要）

---

## 5. 実装配置（リファクタリング計画上の位置）

QED_REVIEW 判定: Note 登録 → **P8 ablation 後に gate-0 を通して採否決定**  
配置候補: FROST ハードゲート追加

gate-0 条件:
1. P8 ablation で既存ゲートの寄与を計測
2. 最小シグナル数ゲートの増分価値（既存ゲートに対する追加 recall/precision 改善）を測定
3. 増分価値が確認されたら FROST ハードゲートに追加

---

## 6. 依存関係

```
NOTE-004 (最小シグナル数ゲート)
    ← P8 軸 ablation（gate-0 評価が先）
    ← FrostEvaluation に signal_count フィールドが必要（なければ schema 追加）
    ※ ADR-002 非依存
    ※ NOTE-001 (DSR) と相補関係（両方あれば小N対策が多層化）
```

---

## 7. 参照

- QED_REVIEW_2026-06-13.md §5 独自追加指摘（第一）
- ADR-002 AlphaEvo 比較（この観点は凍結済みとして登録）
- BSSM（注258）「上位 4% が全利益」知見
- NOTE-001 (DSR) — 相補的な小N対策
