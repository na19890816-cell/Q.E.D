# NOTE-007 — 既存バグの本番影響確認（昇格 Bridge / migration / FROST CLI）

**登録日**: 2026-10-03
**種別**: 運用確認（コード修正は完了済み。本番データへの影響の確認と後始末）
**優先度**: 高（本番データの解釈に影響する）
**ステータス**: Note登録済み・**本番 DB での確認待ち**（サンドボックスからは本番に接続できない）

---

## 1. 修正済みのバグと想定される本番影響

| # | バグ | 修正 commit | 想定される本番影響 |
|---|------|------------|------------------|
| B1 | `promotion_bridge._insert_knowledge_artifact` の不正な f-string 書式 `{x:.4f if ev else 0.0:.4f}` | `36b75e7` | **非 dry_run の EML 昇格は全て例外 → REJECTED (PROMOTION_EXCEPTION)**。EML 由来の knowledge_artifacts は 0 件のはず |
| B2 | migration 079 / 080 / 081 が適用不能 | `32f7248` | 本番に索引・MV・サイズ計測関数が**存在しない**可能性。`init_frost_tables.sh` は 079-081 を適用対象にしていない |
| B3 | FROST CLI (`run_frost_engine.sh`) が `FrostConfig.from_env()` で必ず AttributeError | `32f7248` | CLI 経由の FROST run は 1 件も成功していない |
| B4 | RunContext の非 UUID run_id で FROST の DB 書き込みが必ず失敗 | `32f7248` | `run_frost_pipeline_with_context` 経由の run は frost_runs に残っていない（error_message のみ） |
| B5 | dry_run の FROST run で policy_hash 未記録 | `32f7248` | dry_run 行の `frost_runs.policy_hash IS NULL` |
| B6 | `candidate_hash` が PYTHONHASHSEED 依存 | `0cd3a56` | 同一式でも run ごとに hash が違う。**既存 golden baseline は比較基準として無効** |

## 2. 本番で実行する確認 SQL（読み取りのみ）

```sql
-- B1: EML 昇格の結果内訳（PROMOTION_EXCEPTION が並んでいれば影響あり）
SELECT decision, decision_reason_code, count(*), min(created_at), max(created_at)
  FROM audit_events
 WHERE event_type LIKE 'EML_PROMOTION_%'
 GROUP BY 1, 2 ORDER BY 3 DESC;

SELECT metadata->>'error' AS error, count(*)
  FROM audit_events
 WHERE decision_reason_code = 'PROMOTION_EXCEPTION'
 GROUP BY 1 ORDER BY 2 DESC LIMIT 5;

SELECT count(*) AS eml_artifacts FROM knowledge_artifacts WHERE artifact_type = 'eml_alpha_candidate';

-- B2: 079-081 の適用状況
SELECT filename FROM _migrations WHERE filename LIKE '079%' OR filename LIKE '080%' OR filename LIKE '081%';
SELECT matviewname FROM pg_matviews WHERE matviewname LIKE 'frost_%';

-- B3/B4/B5: FROST run の状況
SELECT status, dry_run, (policy_hash IS NULL) AS no_policy, count(*)
  FROM frost_runs GROUP BY 1, 2, 3 ORDER BY 4 DESC;
```

## 3. 結果に応じた後始末

- **B1 で PROMOTION_EXCEPTION が多数**: 当該候補は「ゲートで落ちた」のではなく「バグで落ちた」。
  再評価が必要なら `run_eml_pipeline.py` を再実行する（現在は shadow ゲートで判定が記録される）。
  過去の REJECTED を手で APPLIED に書き換えることはしない（audit は追記のみ）
- **B2 で未適用**: 修正版 079 → 080 → 081 を適用（全て冪等。空 DB で 2 回適用を確認済み）
- **B6**: golden baseline を修正後のコードで再取得する（`make golden-baseline`）。
  旧 baseline との差分は candidate_hash 列に必ず出るため、比較しても意味がない

## 4. 参照

- `docs/handover/HANDOVER_2026-09-30_session2.md`
- `tests/unit/test_promotion_gates.py::TestPromotionBridge::test_format_bug_fixed`
- `tests/unit/test_p1_repairs.py`
