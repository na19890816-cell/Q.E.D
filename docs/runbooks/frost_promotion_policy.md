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
