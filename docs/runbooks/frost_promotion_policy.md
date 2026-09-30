# FROST Promotion Policy — 昇格ポリシー

**Version**: 1.0.0  
**Last Updated**: 2026-06-10  
**Owner**: ProStock Quant Infrastructure

---

## 1. 概要

FROST Promotion Policy は、FROST 選抜エンジンで `SELECTED` 判定を受けた候補を Q.E.D. の canonical promotion chain へ昇格させるプロセスを定義します。

**重要原則**: FROST は選抜のみを行い、昇格承認は人間のレビューを経ることが原則です。自動昇格は将来的なオプションであり、MVP では review-required default を推奨します。

---

## 2. 昇格フロー

```
frost_selection_decisions
    │
    ├── decision = SELECTED
    │   ├── promotion_eligible = True  (上位 FROST_PROMOTION_TOP_K 件)
    │   │   └── frost_promotion_bridges (promotion_status = 'pending')
    │   │       └── [Quant Review]
    │   │           ├── approved → promotion_status = 'applied' → Q.E.D.
    │   │           └── rejected → promotion_status = 'rejected'
    │   └── promotion_eligible = False (rank > PROMOTION_TOP_K)
    │       └── frost_promotion_bridges (promotion_status = 'dry_run' or なし)
    │
    ├── decision = REVIEW_REQUIRED
    │   └── review_status = 'pending_review'
    │       └── [Quant Review] → review_status = 'approved'
    │           → decision を SELECTED に変更 → 昇格フローへ
    │
    ├── decision = HOLD
    │   └── 次回 FROST 実行の候補として保持
    │
    └── decision = REJECTED
        └── 昇格なし
```

---

## 3. 昇格条件

### 3.1 昇格対象の要件

以下をすべて満たす候補のみが昇格対象になります:

1. `decision = 'SELECTED'`
2. `promotion_eligible = True`
3. `gate_pass = True` (hard gate をすべてクリア)
4. `frost_score IS NOT NULL`
5. `FROST_REQUIRE_AUDIT_PASS = 1` の場合: 関連 audit_events が `APPLIED` ステータスで存在

### 3.2 昇格数制限

| パラメータ | 変数 | デフォルト |
|---|---|---|
| 選抜総数上限 | `FROST_TOP_K` | 25 |
| 昇格数上限 | `FROST_PROMOTION_TOP_K` | 5 |

`FROST_PROMOTION_TOP_K` 以内の SELECTED 候補のみ `promotion_eligible = True` になります。

---

## 4. 昇格ステータス管理

`frost_promotion_bridges.promotion_status` の状態遷移:

```
(新規) → pending
   ├── dry_run=True  → dry_run   (canonical 書き込みなし)
   ├── dry_run=False → pending   (承認待ち)
   │
pending
   ├── 承認 → applied   (Q.E.D. canonical に書き込み完了)
   ├── 却下 → rejected
   └── エラー → error
```

| ステータス | 意味 | DB 書き込み |
|---|---|---|
| `pending` | 昇格承認待ち | frost_promotion_bridges のみ |
| `dry_run` | dry-run 実行 (canonical 非適用) | frost_promotion_bridges のみ |
| `applied` | Q.E.D. 昇格完了 | frost_promotion_bridges + Q.E.D. targets |
| `rejected` | 昇格却下 | frost_promotion_bridges のみ |
| `error` | 昇格エラー | frost_promotion_bridges (error_message 付き) |

---

## 5. dry-run モード

`FROST_DRY_RUN=1` の場合:

- `frost_promotion_bridges.promotion_status = 'dry_run'` (applied にはならない)
- `frost_audit_event_bridges.event_status = 'DRY_RUN'` (APPLIED にはならない)
- `audit_events.decision = 'DRY_RUN'` (APPLIED にはならない)
- Q.E.D. の canonical schema への書き込みは**一切行わない**

```python
# postgres_frost_promotion_bridge.py での実装例
if output.config.dry_run:
    record.promotion_status = "dry_run"
    # frost_promotion_bridges に dry_run レコードを挿入するのみ
else:
    record.promotion_status = "pending"
    # 通常の昇格フローへ
```

---

## 6. Promotion Bridge テーブル

```sql
-- frost_promotion_bridges 主要列
SELECT 
    bridge_id,
    run_id,
    candidate_id,
    trace_id,
    target_entity_type,   -- 'candidate', 'hypothesis', 'knowledge_artifact', 'experiment_report'
    target_entity_id,
    promotion_status,     -- 'pending', 'dry_run', 'applied', 'rejected', 'error'
    promotion_payload_json,
    promoted_at,
    created_at,
    updated_at
FROM frost_promotion_bridges
WHERE promotion_status = 'pending'
ORDER BY created_at ASC;
```

---

## 7. target_entity_type の選択基準

| 昇格先 | `target_entity_type` | 使用シナリオ |
|---|---|---|
| EML 候補式 | `candidate` | EML 由来の alpha 式をそのまま昇格 |
| 仮説として記録 | `hypothesis` | 新規ファクター仮説を Q.E.D. に登録 |
| 知識アーティファクト | `knowledge_artifact` | 研究成果として保存 |
| 実験レポート | `experiment_report` | バックテスト実験記録として保存 |

---

## 8. 昇格実行手順

### 8.1 通常フロー

```bash
# 1. FROST パイプライン実行
make frost-pipeline

# 2. 昇格待ち候補確認
psql "$QED_PG_DSN" -c "
SELECT candidate_id, frost_score, decision_rank
FROM v_frost_promotion_status
WHERE promotion_status = 'pending'
ORDER BY decision_rank ASC;
"

# 3. スコアカードでレビュー
# → v_frost_candidate_scores, v_frost_selection_summary で確認

# 4. 承認 (PostgreSQL 直接更新 or make frost-promote)
UPDATE frost_selection_decisions
SET review_status = 'approved', updated_at = now()
WHERE candidate_id = 'TARGET_CANDIDATE_ID';

# 5. 昇格実行
make frost-promote
```

### 8.2 Makefile 経由

```bash
# 昇格 dry-run (何が昇格されるか確認)
make frost-promote-dry

# 昇格実行
make frost-promote
```

### 8.3 Python 経由

```python
import psycopg
from analytics.python.io.postgres_frost_promotion_bridge import (
    get_pending_promotions,
    update_promotion_status,
    promote_frost_decisions
)
from analytics.python.frost.frost_contracts import FrostRunOutput

with psycopg.connect(pg_dsn) as conn:
    # pending 確認
    pending = get_pending_promotions(conn)
    
    # 個別更新
    update_promotion_status(
        conn,
        run_id="your-run-id",
        candidate_id="your-candidate-id",
        new_status="applied"
    )
```

---

## 9. REVIEW_REQUIRED の処理

borderline 候補 (SELECTED の下位 5%) は `REVIEW_REQUIRED` に昇格します。

### 9.1 REVIEW_REQUIRED → 承認フロー

```bash
# REVIEW_REQUIRED 候補一覧
psql "$QED_PG_DSN" -c "
SELECT 
    sd.candidate_id,
    sd.decision,
    sd.review_status,
    sd.decision_reason,
    fe.frost_score
FROM frost_selection_decisions sd
JOIN frost_evaluations fe USING (candidate_id)
WHERE sd.decision = 'REVIEW_REQUIRED'
  AND sd.review_status = 'pending_review'
ORDER BY fe.frost_score DESC;
"

# 承認
UPDATE frost_selection_decisions
SET review_status = 'approved',
    decision = 'SELECTED',
    updated_at = now()
WHERE candidate_id = 'TARGET_CANDIDATE_ID';

# 次回 frost-promote 実行で昇格
make frost-promote
```

---

## 10. Audit Events との連携

昇格フロー全体で audit_events が発行されます:

| イベント名 | 発行タイミング | audit_events.decision |
|---|---|---|
| `frost.run.started` | FROST 実行開始 | APPLIED |
| `frost.candidate.ingested` | 候補取り込み | APPLIED |
| `frost.candidate.evaluated` | 評価完了 | APPLIED |
| `frost.candidate.selected` | SELECTED 決定 | APPLIED |
| `frost.candidate.rejected` | REJECTED 決定 | REJECTED |
| `frost.promotion.ready` | 昇格準備完了 | APPLIED |
| `frost.run.completed` | FROST 実行完了 | APPLIED |

**dry-run モードでの発行**:
- 同じイベントが発行されるが `decision = 'DRY_RUN'`
- canonical schema への副作用なし

---

## 11. 昇格の取り消し

誤って昇格した候補を取り消す場合:

```sql
-- promotion_status を rejected に更新
UPDATE frost_promotion_bridges
SET promotion_status = 'rejected',
    updated_at = now()
WHERE candidate_id = 'TARGET_CANDIDATE_ID'
  AND promotion_status = 'applied';

-- audit_events に記録 (手動)
INSERT INTO audit_events (
    entity_type, entity_id, event_name, decision,
    trace_id, payload_json, occurred_at
) VALUES (
    'frost_candidate',
    'TARGET_CANDIDATE_ID',
    'frost.promotion.revoked',
    'REJECTED',
    'YOUR_TRACE_ID',
    '{"reason": "manual revocation"}',
    now()
);
```

---

## 12. 将来の自動昇格

十分な監査実績が蓄積されたら、以下の条件を満たす候補は自動昇格を検討できます:

- `frost_score ≥ 0.80`
- `pbo_score ≤ 0.05`
- `decision_rank ≤ 3`
- 過去 90 日間の類似候補の OOS 実績が良好

**現時点では自動昇格は実装しない**。すべての昇格に人間のレビューを必須とする。

---

## 13. 関連ドキュメント

- [frost_engine.md](frost_engine.md) — Engine Operations Runbook
- [frost_scorecard.md](frost_scorecard.md) — スコアカード詳細仕様
- [frost_failure_modes.md](frost_failure_modes.md) — 障害モードと対処

---

## 14. Champion–Challenger 運用ルール

**追記日**: 2026-06-13  
**出典**: QED_REVIEW_2026-06-13.md §4 トリアージ #5（コード変更不要・明文化のみ）

### 14.1 概要

Champion–Challenger は、SELECTED 判定を受けた新規アルファを **即時昇格させず**、  
一定期間だけ display-only（参照のみ）で既存ポートフォリオと並走させる運用ルールである。  
fomoEngine（注259）で既に実践しているパターンを、昇格ポリシーとして明文化する。

新規昇格候補 = **Challenger**、現行採用アルファ = **Champion** と呼ぶ。

### 14.2 運用フロー

```
FROST 評価
    └── decision = SELECTED + promotion_eligible = True
            │
            ▼
    [Challenger 期間開始]
    promotion_status = 'challenger'  (display-only、canonical 非適用)
            │
            ├── N 営業日の並走観察
            │   └── 実績 IC / rolling SR / 採用済みアルファとの相関を記録
            │
            ├── 観察期間中に FROST 再評価（任意）
            │
            ▼
    [Quant レビュー]
            ├── Challenger が Champion に対して優位 → promotion_status = 'pending' → 通常昇格フローへ
            ├── 優位性が確認できない → promotion_status = 'rejected'
            └── 引き続き観察 → 期間延長（最大 2N 営業日）
```

### 14.3 Challenger 期間の推奨設定

| パラメータ | 推奨値 | 根拠 |
|---|---|---|
| 並走期間 N | **21 営業日（約 1 ヶ月）** | 十分なサンプルで IC を評価できる最短期間 |
| 延長上限 | 42 営業日（2N） | これ以上延ばすと判断が先送りになる |
| 評価指標 | rolling IC (21 日)、累積 PnL、採用済み相関 | FROST のスコア軸と整合 |

### 14.4 promotion_status 拡張

Champion–Challenger 運用のために `frost_promotion_bridges.promotion_status` に  
`challenger` 状態を追加する（**将来実装オプション**。現時点では手動管理でも可）。

```
現状の状態遷移（§4）:
  pending → applied / rejected / error / dry_run

Champion–Challenger 拡張後:
  pending
    └── challenger  (display-only、N 日並走)
            ├── 優位確認 → pending → applied
            └── 優位なし → rejected
```

**注意**: `promotion_status = 'challenger'` の DB 追加は Schema 変更を伴うため、  
現時点では `promotion_status = 'dry_run'` + 手動台帳（スプレッドシート等）で代替可能。  
将来の自動化フェーズで正式に追加する。

### 14.5 Champion 置き換えの原則

Challenger が Champion を置き換える場合の判断基準:

1. **FROST スコア**: Challenger の frost_score ≥ Champion の frost_score × 1.05（5% 超過）
2. **OOS 相関**: Challenger と Champion の相関 r < 0.6（NOTE-002 ゲートと同一基準）
3. **rolling IC**: Challenger の直近 21 日 IC が Champion の同期間 IC を上回る
4. **FSI（摂動安定性）**: Challenger の FSI が Champion と同等以上

上記 4 条件をすべて満たす場合のみ置き換えを推奨する。  
いずれかを満たさない場合は「並存」（既存 Champion を維持しつつ Challenger も昇格）を検討する。

### 14.6 実施手順（現行 = 手動管理）

```bash
# 1. Challenger として仮登録（dry_run で昇格 Bridge を作成）
FROST_DRY_RUN=1 make frost-promote

# 2. N 営業日の観察記録（手動台帳 or スプレッドシート）
#    記録項目: 日付, candidate_id, rolling_ic, cum_pnl, corr_vs_champion

# 3. N 日後に Quant レビュー → 昇格 or 棄却
#    昇格の場合: promotion_status を pending に変更後、make frost-promote
#    棄却の場合: promotion_status を rejected に更新

# 4. Champion 置き換えの場合
UPDATE frost_promotion_bridges
SET promotion_status = 'revoked', updated_at = now()
WHERE candidate_id = '<旧 Champion ID>'
  AND promotion_status = 'applied';
```

### 14.7 このルールの意義

Champion–Challenger は「昇格の一方通行性」を緩和する最初のステップである。  
完全な Detect → Kill ライフサイクル（NOTE-003）の実装前において、  
「新規候補を慎重に評価する」という文化的・運用的なガードとして機能する。

> FROSTが候補アルファに取っている態度（勝った実績ではなく検証を通ったかで判断する）を、  
> 昇格後のアルファの「後継選択」にも適用する。— QED_REVIEW_2026-06-13.md §2

---

## 15. Detect → Kill ライフサイクル (NOTE-003 / 憲法ギャップ G3)

**Last Updated**: 2026-09-30  
**実装モジュール**: `analytics/python/frost/frost_cusum.py`, `analytics/python/frost/frost_lifecycle.py`

### 15.1 概要

昇格済みアルファが時間の経過とともに性能劣化した場合に検知し、KillQueue に追加して  
人間レビューを経た上で昇格を取り消す（Kill する）フローを定義する。

設計憲法のシグナルライフサイクル全体:

```
Predict → Select → Execute → Detect → Kill
                                 ↑
                             本セクションのスコープ
```

**半自動 Kill 方針**: CUSUM が劣化を検知しても、即時自動 Kill は行わない。  
人間の最終承認（Quant レビュー）を経てから `REVOKED` に遷移させる。  
これは「承認なき降格」を防ぐ安全装置である。

### 15.2 LifecycleStatus（状態遷移）

```
ACTIVE
  │
  ├── CUSUM 劣化検知 ──→ DEGRADED (review_required=True)
  │                           │
  │                           ├── Quant Review: Kill 承認 ──→ REVOKED
  │                           ├── Quant Review: 継続承認 ──→ ACTIVE (復帰)
  │                           └── 未レビュー中 ──→ UNDER_REVIEW (任意)
  │
  ├── Champion-Challenger 並走 ──→ SUSPENDED
  │       └── Challenger 勝利 ──→ REVOKED
  │       └── Champion 維持 ──→ ACTIVE (復帰)
  │
  └── (直接) ──→ REVOKED (手動 Kill)
```

| 状態 | 意味 | 再検査対象 |
|------|------|-----------|
| `active` | 正常稼働中 | ✅ |
| `degraded` | CUSUM Detect トリガー済み・レビュー待ち | ✅ |
| `under_review` | 人間レビュー中 | ❌ スキップ |
| `revoked` | Kill 確定（昇格取消） | ❌ スキップ |
| `suspended` | Champion-Challenger 並走中 | ✅ |

### 15.3 CUSUM 劣化検知パラメータ

**実装**: `CusumDetector` (Page 1954 双方向 CUSUM、純 Python / ADR-001 準拠)

| パラメータ | デフォルト | 意味 |
|-----------|-----------|------|
| `k` | 0.5 | 許容ドリフト（中立点 = `mu0 - k`） |
| `h` | 5.0 | 警告閾値（累積和がこれを超えたら Detect） |
| `mu0` | 0.0 | 正常時の IC 期待値 |
| `min_ic_len` | 10 | CUSUM 実行に必要な最小 rolling IC 点数 |

**数理仕様 (Page 1954)**:

```
下方 CUSUM: S_neg[t] = max(0, S_neg[t-1] - (v[t] - mu0 + k))
上方 CUSUM: S_pos[t] = max(0, S_pos[t-1] + (v[t] - mu0) - k)

下方 Detect: S_neg[t] >= h  → 劣化検知 (IC が持続的に低下)
上方 Detect: S_pos[t] >= h  → 異常回復検知 (データ品質問題の代理指標)
```

**中立点の解釈**: IC が `mu0 - k`（デフォルト: `-0.5`）を上回る限り S_neg は増加しない。  
IC が中立点を下回って初めて、その差分が S_neg に蓄積される。

### 15.4 Kill フロー（半自動）

```
[毎バッチ実行または on-demand]
        │
        ▼
AlphaLifecycleEngine.build_kill_queue(promoted_records)
        │
        ├── 各アルファ: AlphaLifecycleEngine.check(record)
        │       ├── REVOKED / UNDER_REVIEW → スキップ
        │       ├── rolling_ic < min_ic_len → スキップ
        │       └── CusumDetector.run(rolling_ic)
        │               ├── degradation_detected=True
        │               │   → new_status=DEGRADED, review_required=True
        │               └── degradation_detected=False
        │                   → new_status 維持 (DEGRADED→ACTIVE 復帰含む)
        │
        ▼
KillQueue (review_required=True のアルファを収集)
        │
        ▼
[Quant Review — 人間の判断]
        ├── Kill 承認 → promotion_status='revoked' に更新 (pg_io 経由)
        ├── 継続承認 → promotion_status='active' に戻す
        └── 追加観察 → promotion_status='under_review' に設定
```

### 15.5 実施手順

#### 15.5.1 Python から実行

```python
from analytics.python.frost.frost_lifecycle import (
    AlphaLifecycleEngine, LifecycleRecord, LifecycleStatus
)
from analytics.python.frost.frost_cusum import CusumParams

# エンジン初期化
engine = AlphaLifecycleEngine(
    params=CusumParams(k=0.5, h=5.0, mu0=0.0),
    min_ic_len=10,
)

# DB から昇格済みアルファと rolling IC を取得（pg_io 経由）
records = [
    LifecycleRecord(
        artifact_id=row["artifact_id"],
        current_status=row["promotion_status"],
        rolling_ic=row["rolling_ic_series"],  # List[float]
    )
    for row in promoted_artifacts_with_ic
]

# KillQueue を構築
kill_queue = engine.build_kill_queue(records)

print(f"要レビュー件数: {kill_queue.count}")
for item in kill_queue.pending_reviews:
    print(f"  {item.artifact_id}: {item.reason}")
    # CUSUM 詳細も取得可能
    if item.cusum_result:
        print(f"    first_degradation_index={item.cusum_result.first_degradation_index}")
```

#### 15.5.2 FrostConfig / PolicySpec から初期化

```python
from analytics.python.frost.frost_lifecycle import AlphaLifecycleEngine

# PolicySpec や FrostConfig から自動設定
engine = AlphaLifecycleEngine.from_config(policy_spec)
# policy_spec に cusum_k, cusum_h, cusum_mu0, lifecycle_min_ic_len 属性があれば使用
```

### 15.6 Kill 実行後の後処理

REVOKED 確定後（`pg_io` 経由）に以下を実施する:

1. **knowledge_artifacts**: `promotion_status = 'revoked'` に更新
2. **frost_promotion_bridges**: `promotion_status = 'revoked'` に更新
3. **Audit Event 記録**: `event_type = 'alpha_killed'`、理由・CUSUM スコアを記録
4. **Champion-Challenger**: Kill されたアルファが Champion だった場合、Challenger を新 Champion に昇格
5. **ポートフォリオ相関再検査**: Kill により promoted_signals が変化するため、G2 ゲートを再実行

### 15.7 パラメータチューニング指針

| シナリオ | 推奨調整 |
|----------|---------|
| 誤検知が多い（正常アルファが DEGRADED になる）| `h` を大きくする（感度低下） |
| 検知が遅い（明らかな劣化を見逃す） | `h` を小さくする または `k` を小さくする |
| IC が 0 近傍でなく正の期待値を持つ | `mu0` を実績 IC 平均値に調整 |
| 短期 rolling IC しか持てない | `min_ic_len` を引き下げる（慎重に） |

> **注意**: パラメータ変更は `PolicySpec` 経由で行い（`cusum_k`, `cusum_h`, `cusum_mu0`, `lifecycle_min_ic_len`）、  
> 変更後は `policy_hash` が更新される。既存の KillQueue の判定は旧パラメータで行われたものとして扱う。

### 15.8 このルールの意義

G3 Detect → Kill ライフサイクルは、設計憲法の最終フェーズを実装する。  
これにより FROST の役割は「昇格判断」だけでなく「昇格後モニタリング」にも拡張され、  
Q.E.D. システム全体の統計的健全性を長期的に維持する仕組みが整う。

> 「昇格した後のアルファも、FROST が引き続き監視する」— NOTE-003 §1

---

## 16. Deflated Sharpe Ratio ゲート (NOTE-001 / 憲法ギャップ G1)

> 実装: `analytics/python/frost/frost_dsr.py` (Phase 4b, 2026-09-30)

### 16.1 概要

昇格決定時に候補の OOS リターン系列から DSR を算出し、`DSR >= min_dsr` を昇格条件とする。
G2 (PortfolioCorrelationGate) と同様、FROST 評価時ではなく **昇格 Bridge レイヤーから呼ぶスタンドアロンゲート**。
gate_engine / スコア軸への統合は P8 ablation 後に判断する。

```
DSR = PSR(SR0)
PSR(SR*) = Φ((SR^ − SR*)·√(T−1) / √(1 − γ3·SR^ + (γ4−1)/4·SR^²))
SR0 = √V[SR] · ((1−γ)·Φ⁻¹(1−1/N) + γ·Φ⁻¹(1−1/(N·e)))
```

### 16.2 パラメータ (PolicySpec.hard_gates)

| フィールド | 環境変数 | デフォルト | 意味 |
|---|---|---|---|
| `min_dsr` | `FROST_MIN_DSR` | 0.95 | DSR 合格閾値 |
| `dsr_default_n_trials` | `FROST_DSR_DEFAULT_N_TRIALS` | 1 | N 未提供時の仮定試行数 |

### 16.3 試行回数 N の扱い (ADR-002 未整備への暫定措置)

| 入力 | n_trials_source | review_required |
|---|---|---|
| `n_trials` 明示 | `provided` | 不合格時のみ True |
| 未指定 (`dsr_default_n_trials` を使用) | `assumed` | **常に True** |
| `trial_sharpes` のみ | `assumed` (本数を N の下限として採用) | **常に True** |

N の過少申告は DSR を楽観化するため、仮定値での合格は必ず人間レビューを経る。
ADR-002 系譜ログで run 横断の累積試行数が集計可能になった時点で `n_trials` を明示供給に切り替える。

### 16.4 使用例

```python
from analytics.python.frost.frost_dsr import DsrGate
gate = DsrGate.from_config(policy_spec)
res = gate.check(oos_daily_returns, n_trials=trial_count, trial_sharpes=sibling_srs)
if not res.passed:
    reject(res.failure_reason)          # DSR_BELOW_THRESHOLD / INSUFFICIENT_OBS
elif res.review_required:
    enqueue_review(res.to_dict())
```

- リターンは **非年率** (日次等) のまま渡す。SR も非年率で計算される。
- `trial_sharpes` を渡す場合は同じ頻度の非年率 SR で揃えること。
