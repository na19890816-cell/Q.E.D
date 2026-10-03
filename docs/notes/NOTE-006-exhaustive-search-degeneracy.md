# NOTE-006 — exhaustive 探索の縮退（93,347 木 → 17 式）

**登録日**: 2026-10-03
**出典**: ADR-002 S1 実装時の実測（ADR-002 §4.7）
**種別**: 探索設計の課題（憲法ギャップではない）
**優先度**: 中
**ステータス**: Note登録済み・未着手
**コード変更**: なし（Note登録のみ）

---

## 1. 事実

`eml_search.exhaustive_search` は depth ≤ 2 の全 EML 木を列挙して fitness を評価するが、
列挙された木の EML セレクタは `raw_weight = 0` のまま（学習しない）。

```
sigmoid(0) = 0.5  →  compile_to_expr: w >= 0.5 → 左を選択
```

このため全ての木が「最左の葉」に縮退する。

| 実測（16 端子, depth ≤ 2） | 値 |
|---|---|
| 評価した木 | 93,347 |
| 異なる compiled_expr | **17**（端子 16 + 定数 `1.0`） |
| 4 端子・depth ≤ 2 | 875 木 → 5 式 |

**exhaustive の計算のほぼ全て（> 99.98%）が同一式の再評価**であり、探索としての情報量は
「各端子を単体で評価する」のと同じである。

## 2. 影響

- 計算資源: 実行時間の大半（実測で run_eml_pipeline の約 2 分の大部分）が冗長評価
- 探索空間: 二項の組み合わせ（`eml(a, b)` で右を選ぶ木）が exhaustive からは一切生まれない
- DSR: ADR-002 で試行 = 異なる式と定義したため、N の過大計上は回避済み（影響なし）

## 3. 選択肢（未評価）

| 案 | 内容 | 懸念 |
|---|---|---|
| A | exhaustive の列挙を「端子単体」に縮める（現状の実効挙動を明示化） | 挙動不変・計算削減のみ。golden 不変 |
| B | セレクタ重みも列挙する（各 EML ノードで左/右の 2 通り） | 2^ノード数 で爆発。depth 2 なら現実的か要測定 |
| C | 列挙後に snap 前の短い学習を入れる（gradient と統合） | gradient_search と役割が重複 |

**扱い**: リファクタリング計画の不変条件（機能追加とリファクタの分離）に従い、案 A は golden 不変の
性能改善として、案 B/C は探索仕様の変更として gate-0 を通してから入れる。
案 B/C を入れると異なる式の数が増え、DSR の N も増える（=選抜が厳しくなる）点に留意。

## 4. 参照

- ADR-002 §4.7 / `tests/unit/test_adr002_s1_search_stats.py::test_exhaustive_degeneracy_documented`
- `analytics/python/alpha/eml/eml_compiler.py::compile_to_expr`
