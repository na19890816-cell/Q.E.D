# NOTE-003 — 憲法ギャップ G3: Detect → Kill ライフサイクル未実装

**登録日**: 2026-06-13  
**出典**: QED_REVIEW_2026-06-13.md §3「憲法ギャップ監査 — ギャップ3」  
**種別**: 憲法ギャップ（外部提案ではなく、設計憲法が既に義務付けている未実装項目）  
**優先度**: 高（憲法違反・思想と実装の乖離）  
**ステータス**: ✅ 実装完了 (2026-09-30)  
**コード変更**: `analytics/python/frost/frost_cusum.py`, `analytics/python/frost/frost_lifecycle.py` 新規作成

---

## 1. ギャップの概要

設計憲法のシグナルライフサイクルは以下を定義している:

```
Predict → Select → Execute → Detect → Kill
```

**現状**: QED が実装しているのは `Select` まで。昇格は **一方通行**。

```
現在:  Predict → Select → Execute   (Detect も Kill も存在しない)
憲法:  Predict → Select → Execute → Detect → Kill
```

---

## 2. なぜ深刻か

「アルファは壊れる」という前提はこのシステムの生存ファーストの思想の根幹である。  
FROSTが候補を厳しく選抜する理由は「過去に勝ったから」ではなく「摂動に対して安定だから」であり、  
その同じ思想は昇格後のアルファにも適用されなければならない。

**Kill の欠如 = 思想と実装の乖離**:
- FROST は候補を厳密にゲートして昇格させる
- しかし昇格した瞬間、そのアルファは永続的に保護される
- 環境が変化してアルファが劣化しても、自動的には降格しない

---

## 3. 実装コンセプト

### 3.1 Detect（劣化検知）

昇格済みアルファの rolling IC（情報係数）に CUSUM（累積和制御チャート）を適用し、  
統計的に有意な劣化を検知する。

```python
# CUSUM on rolling IC — 疑似コード（実装時の参考）
def detect_degradation_cusum(
    rolling_ic: List[float],
    k: float = 0.5,       # 許容ドリフト量（IC の標準偏差の半分が典型値）
    h: float = 5.0,       # 警告閾値（累積和がこれを超えたら Detect）
) -> bool:
    """
    CUSUM（下方向）: IC が継続的に低下しているかを検知する。
    純 Python 実装（ADR-001 準拠、numpy 不使用）。
    """
    cusum_neg = 0.0
    for ic in rolling_ic:
        cusum_neg = max(0.0, cusum_neg - ic - k)
        if cusum_neg >= h:
            return True  # 劣化検知
    return False
```

### 3.2 Kill（自動降格）

Detect トリガー後、アルファを降格候補としてキューに積み、  
人間レビューを経て `promotion_status = 'revoked'` に遷移させる。  
完全自動 Kill は人間レビュー省略になるため、**現時点では自動化しない**。

```
昇格済みアルファ
    └── rolling IC 計算（定期実行）
            └── CUSUM 検知 → degradation_detected = True
                    └── [Quant レビュー]
                            ├── 降格確認 → promotion_status = 'revoked'
                            └── 継続確認 → degradation_detected = False にリセット
```

---

## 4. 技術的実現性

- **実装コスト**: 低。CUSUM は純 Python で書ける（ADR-001 準拠）
- **データ要件**: rolling IC の時系列（OOS での実績 IC）
- **インフラ要件**: 定期実行ジョブ（Makefile ターゲット追加で対応可能）

QED_REVIEW の評価: 「rolling IC への CUSUM 等は純 Python で軽く書ける。採用。」

---

## 5. 実装配置（リファクタリング計画上の位置）

QED_REVIEW トリアージ判定: **採用（リファクタ完了後の最初の機能追加として計画）**  
配置: **リファクタリング計画 P0〜P9 完了後の最初の機能追加フェーズ**

新規モジュール候補:
```
analytics/python/frost/frost_lifecycle.py    — Detect/Kill ロジック
analytics/python/frost/frost_cusum.py        — CUSUM 実装（純 Python）
docs/runbooks/frost_lifecycle.md             — ライフサイクル運用 Runbook
```

---

## 6. 依存関係

```
NOTE-003 (Detect → Kill)
    ← リファクタリング P0〜P9 完了（先行）
    ← rolling IC の時系列データ蓄積
    ← frost_promotion_policy.md への Kill フロー追記（NOTE-003 実装時）
    ※ ADR-002 非依存 → NOTE-001 より先行実装可能
```

---

## 7. 参照

- QED_REVIEW_2026-06-13.md §3 ギャップ3
- QED_REVIEW_2026-06-13.md §4 トリアージ #3（レビューA「Concept Drift 検知 + 自動降格」提案と同一）
- 設計憲法（Detect → Kill ライフサイクル義務）
- `frost_promotion_policy.md` — 昇格フロー定義（Kill フローは未記載）
- Page, E.S. (1954). "Continuous inspection schemes." Biometrika 41(1), 100–115. (CUSUM 原著)

---

## 8. 実装記録 (2026-09-30)

### 作成ファイル

| ファイル | 説明 |
|----------|------|
| `analytics/python/frost/frost_cusum.py` | Page (1954) 双方向 CUSUM — 純 Python 実装 (ADR-001 準拠) |
| `analytics/python/frost/frost_lifecycle.py` | Detect/Kill ライフサイクル管理エンジン |
| `tests/unit/test_phase3_detect_kill.py` | 105 tests / 10 クラス / `@pytest.mark.phase3_detect_kill` |

### 実装の重要決定事項

1. **CUSUM 数式**: Page (1954) の正式定式化を採用
   - 下方: `S_neg[t] = max(0, S_neg[t-1] - (v[t] - mu0 + k))`
   - 中立点 = `mu0 - k` (デフォルト: `-0.5`)
   - `v > mu0 - k` の場合 S_neg は増加しない（正常範囲として無視）

2. **半自動 Kill 方針**: CUSUM 検知 → KillQueue 追加 → 人間承認の 3 段階
   - 即時自動 Kill は実装しない（NOTE-003 §3.2 方針を遵守）

3. **スキップ対象**: `REVOKED` / `UNDER_REVIEW` は再検査しない
   - 人間レビュー中のアルファを二重処理しないための安全装置

4. **DEGRADED → ACTIVE 復帰**: CUSUM が一度検知しても次回チェックで未検知なら復帰
   - CUSUM は全系列に対してリセットから再計算するため、直近のデータが正常なら復帰する

### テスト構成 (105 tests / 10 クラス)

| クラス | Tests | 内容 |
|--------|-------|------|
| `TestCusumParams` | 9 | パラメータバリデーション・from_config |
| `TestCusumStepResult` | 5 | step 結果プロパティ |
| `TestCusumRunResult` | 7 | バッチ結果プロパティ・to_dict |
| `TestCusumDetectorStep` | 10 | ストリーミング動作・NaN/Inf ガード |
| `TestCusumDetectorRun` | 10 | バッチ処理・混合シナリオ |
| `TestDetectDegradationCusum` | 6 | 関数型ラッパー |
| `TestLifecycleStatus` | 4 | 定数 |
| `TestLifecycleRecord` | 4 | 入力 DTO |
| `TestLifecycleCheckResult` | 5 | 結果プロパティ・to_dict |
| `TestKillQueue` | 6 | キュー管理・to_dict |
| `TestAlphaLifecycleEngineCheck` | 14 | ステータス遷移ロジック |
| `TestAlphaLifecycleEngineBatch` | 8 | check_batch / build_kill_queue |
| `TestAlphaLifecycleEngineFromConfig` | 5 | from_config |
| `TestCheckAlphaDegradation` | 6 | 関数型ラッパー |
| `TestIntegration` | 6 | 統合シナリオ |

### runbook 追記

`docs/runbooks/frost_promotion_policy.md` §15 に Kill フロー全体を追記。
