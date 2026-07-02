# ADR-001: numpy 解禁ポリシー

**Status**: Accepted — 2026-06-13
**Decision maker**: Nao
**Context**: QEDリファクタリング計画 Q1 への回答(「EC2実行ならnumpy解禁可能」)

## 決定

numpy の使用を許可する。ただし以下の3条件付き。

### 条件1: 導入は Phase 7 のみ。今は1行も書かない

golden harness(Phase 0)による正しさの凍結が完了し、Phase 1–6 で
Decision が純関数化されるまで、numpy 化は行わない。
理由: 浮動小数点の集約順序差(numpy の sum と純Python の sum は
結果が微妙に異なりうる)を、検出器なしで持ち込むのは計器なし飛行。

### 条件2: 適用範囲はホットパスのみ(ホワイトリスト方式)

| 許可 | モジュール | 理由 |
|------|-----------|------|
| ✅ | frost_pbo / frost_pbo_parallel | CPCV 組合せの行列演算 |
| ✅ | alpha_genome_cluster (KMeans++) | 距離計算の全ペア演算 |
| ✅ | alpha_genome_similarity (cosine) | ベクトル演算 |
| ✅ | frost_crowding (OLS) | 線形代数 |
| ✅ | frost_fragility_surface | 摂動グリッド評価 |
| ❌ | frost_contracts / frost_config | 依存を増やす理由がない |
| ❌ | frost_decision_engine / frost_selector | 決定経路は依存最小を維持 |
| ❌ | io/ 全 writer | 同上 |
| ❌ | eml_core / eml_compiler | AST 安全証明の経路は触らない |

実際のプロファイル結果(Phase 7 冒頭で cProfile 取得)により、
ホワイトリストから**削る**ことはあっても増やさない。

### 条件3: 数値同等性の許容基準

- **決定(SELECTED/HOLD/REJECTED/REVIEW_REQUIRED)**: 反転ゼロ。例外なし
- **frost_score / 各軸スコア**: 相対誤差 1e-8 以内
  (golden harness の ROUND_DECIMALS=8 で比較)
- 基準を満たせないモジュールは numpy 化を見送り、純Python のまま残す

## 設計憲法との整合

- 「Entry is technique, but exit is discipline」 → 性能改善(entry)より
  正しさの維持(discipline)が優先。条件1がこれに対応
- 「バックテストは仮説検証の道具」 → numpy 化の前後比較自体を
  golden harness で検証するため、最適化も検証可能な仮説として扱う

## Termux に関する注記

開発端末(Termux)では numpy 非搭載でもユニットテストの大半が
走るよう、numpy 依存テストには pytest marker `@pytest.mark.numpy_accel`
を付け、`-m "not numpy_accel"` でスキップ可能にする(Phase 7 で実施)。
フルスイートの正は EC2 とする。

## 却下した代替案

- **numpy/純Python の二重実装(フォールバック付き)**: 分岐維持コストが
  use_v2_score フラグと同型の負債を生む。却下
- **即時全面 numpy 化**: 条件1の理由により却下
