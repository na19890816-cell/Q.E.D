# NOTE-002 — 憲法ギャップ G2: 採用済みポートフォリオとの相関ゲート r<0.6 未実装

**登録日**: 2026-06-13  
**実装日**: 2026-09-30  
**出典**: QED_REVIEW_2026-06-13.md §3「憲法ギャップ監査 — ギャップ2」  
**種別**: 憲法ギャップ（外部提案ではなく、設計憲法が既に義務付けている未実装項目）  
**優先度**: 高（憲法違反）  
**ステータス**: ✅ 実装完了 — `portfolio_correlation_gate.py` / `policy_spec.py`・`frost_config.py` 更新済み

---

## 1. ギャップの概要

設計憲法は「採用済みポートフォリオとの相関 r < 0.6」の昇格ゲートを要求している。

**現状の誤解を招く実装**:
- `max_signal_corr ≦ 0.90` は **候補同士** の重複排除（DedupStage）
- 候補対 **採用済みアルファ** の相関チェックは **存在しない**

```
現在の実装:          候補A ↔ 候補B  の相関チェック ✅
憲法が要求:  候補A ↔ 採用済みアルファ の相関チェック ❌ (未実装)
```

---

## 2. なぜ必要か

ポートフォリオ分散効果の喪失を防ぐ。  
いくら候補同士の重複を排除しても、既存採用アルファと高相関なシグナルが昇格すれば  
ポートフォリオとしての多様性が低下し、共通要因リスクが集中する。

例: 採用済みアルファが5本あり、そのすべてと新規候補の相関が r=0.85 の場合、  
候補同士の DedupStage をクリアしても、ポートフォリオレベルでは重複リスクがある。

---

## 3. 実装可能性（既存資産で対応可能）

`knowledge_artifacts` に昇格済みアルファの OOS シグナルが保存されている。  
追加インフラ不要で実装できる。

### 実装候補手順

```python
# 昇格前ゲートの疑似コード（実装時の参考）
def check_portfolio_correlation_gate(
    candidate_oos_signal: List[float],
    promoted_artifacts: List[KnowledgeArtifact],
    threshold: float = 0.6,
) -> GateResult:
    """
    候補の OOS シグナルと採用済みアルファ全本との相関を計算し、
    いずれかが threshold を超えた場合にゲート失敗とする。
    """
    for artifact in promoted_artifacts:
        r = pearson_correlation(candidate_oos_signal, artifact.oos_signal)
        if abs(r) >= threshold:
            return GateResult(
                passed=False,
                reason=f"OOS 相関 r={r:.3f} >= {threshold} (artifact={artifact.id})"
            )
    return GateResult(passed=True)
```

---

## 4. 実装配置（リファクタリング計画上の位置）

QED_REVIEW トリアージ判定: **採用**  
配置候補:

| 配置 | タイミング |
|---|---|
| FROST 昇格ゲート層（`frost_promotion_policy` レイヤー） | 昇格 Bridge 実行直前 |
| FROST ハードゲート追加（`frost_contracts` Gate 定義） | FROST 評価時 |

**推奨**: 昇格前ゲート（昇格 Bridge レイヤー）。  
理由: 採用済みポートフォリオ情報は FROST 評価時点では不要。昇格決定時に初めて意味を持つ。

実装優先度: P8 ablation 完了後。ADR-002 系譜ログ非依存のため NOTE-001 より先行可能。

---

## 5. 依存関係

```
NOTE-002 (相関ゲート r<0.6)
    ← knowledge_artifacts の OOS シグナル（既存資産で対応可能）
    ← P8 軸 ablation（既存ゲート評価後に追加）
    ※ ADR-002 非依存 → NOTE-001 より先行実装可能
```

---

## 6. 参照

- QED_REVIEW_2026-06-13.md §3 ギャップ2
- QED_REVIEW_2026-06-13.md §4 トリアージ #2（レビューA「Portfolio-Level Validation」提案と同一）
- 設計憲法（r<0.6 相関ゲート義務）
- `analytics/python/frost/dedup_stage.py` — 現行の候補同士重複排除（DedupStage）
- `frost_promotion_policy.md` — 昇格フロー定義

---

## 7. 実装記録 (2026-09-30)

### 変更ファイル

| ファイル | 変更内容 |
|---|---|
| `analytics/python/frost/portfolio_correlation_gate.py` | **新規作成** — G2 ゲート本体 |
| `analytics/python/frost/policy_spec.py` | `max_portfolio_corr: float = 0.60` フィールド追加 (to_dict / from_dict / from_frost_config / to_frost_config / load_policy_spec) |
| `analytics/python/frost/frost_config.py` | `max_portfolio_corr: float = 0.60` フィールド追加 (from_env) |
| `tests/unit/test_phase2_portfolio_correlation_gate.py` | **新規作成** — 62 tests / 8 クラス |
| `tests/unit/test_phase1_policy_spec.py` | `test_to_dict_hard_gates_count` を 15→16 に更新 |

### 公開 API

```python
from analytics.python.frost.portfolio_correlation_gate import (
    PortfolioCorrelationGate,         # クラス API
    check_portfolio_correlation_gate, # 関数型ラッパー
    PortfolioGateResult,
    SingleCorrResult,
)

gate = PortfolioCorrelationGate.from_config(policy_spec)
result = gate.check(
    candidate_signal=[...],
    promoted_signals={"artifact_id": [...], ...},
)
if not result.passed:
    print(result.failure_reason)      # "OOS 相関 r=0.8500 が閾値 0.6000 を超過..."
    print(result.to_dict())           # FrostEvaluation.diagnostics_json 格納用
```

### テスト結果

```
62 passed in 0.27s ✅ (全テストスイート: 1056 passed, 24 skipped)
```
