# Event Study Pipeline — 運用 Runbook

**対象システム**: prostock-bff-v2 / Q.E.D. Event Study パイプライン  
**対象環境**: EC2 t3.micro (30 GB gp3), PostgreSQL (qed_dev), DuckDB + Parquet  
**最終更新**: 2026-04-13

---

## 目次

1. [概要・フロー](#1-概要フロー)
2. [前提条件・初期セットアップ](#2-前提条件初期セットアップ)
3. [環境変数リファレンス](#3-環境変数リファレンス)
4. [Phase 別実行手順](#4-phase-別実行手順)
5. [統合実行（bootstrap master）](#5-統合実行bootstrap-master)
6. [検証コマンド](#6-検証コマンド)
7. [テスト実行](#7-テスト実行)
8. [トラブルシューティング](#8-トラブルシューティング)
9. [再実行・べき等性](#9-再実行べき等性)
10. [既知の制約と拡張予定](#10-既知の制約と拡張予定)

---

## 1. 概要・フロー

```
DuckDB (Parquet)
      │
      ▼ Phase A
event_study_summary_runs
event_study_summaries
      │
      ▼ Phase B
event_study_experiment_report_bridge
experiment_runs (QED)
      │
      ▼ Phase C
knowledge_artifacts
      │
      ▼ Phase D
target_resolution_log  ──→  factor_candidates / hypotheses (解決先)
      │
      ▼ Phase E
artifact_links
      │
      ▼ Phase F
audit_events (QED)
event_study_pipeline_audit
```

**trace_id** は Phase A で UUID5 として決定論的に生成され、全ステージで引き継がれます。

---

## 2. 前提条件・初期セットアップ

### 必要ソフトウェア

```bash
python3 --version    # 3.10 以上
psql --version       # PostgreSQL クライアント
```

### Python 依存パッケージのインストール

```bash
cd ~/prostock-bff-v2   # または /home/user/prostock
pip install psycopg[binary] pandas numpy
# オプション: DuckDB 実 Parquet を使う場合
pip install duckdb
```

### 環境変数の準備

```bash
# テンプレートをコピーして編集
cp config/env/.env.example config/env/.env.local
vi config/env/.env.local   # QED_PG_DSN 等を設定

# または make を使用
make setup
```

### マイグレーション適用

```bash
# 015〜021 を一括適用
for f in qedschema/migrations/01{5,6,7,8}_*.sql qedschema/migrations/02{0,1}_*.sql; do
  psql "$QED_PG_DSN" -f "$f" && echo "OK: $f"
done

# seed データ投入
psql "$QED_PG_DSN" -f qedschema/seeds/020_event_study_target_rule_seed.sql

# ビュー作成
for f in qedschema/views/*.sql; do
  psql "$QED_PG_DSN" -f "$f" && echo "OK: $f"
done
```

---

## 3. 環境変数リファレンス

| 変数名 | 必須 | デフォルト | 説明 |
|--------|------|-----------|------|
| `QED_PG_DSN` | ✅ | — | PostgreSQL 接続文字列 |
| `EVENT_AR_OUTPUT_PATH` | — | `data/event_study/ar_panel.parquet` | DuckDB 出力 Parquet パス |
| `EVENT_STUDY_SOURCE_NAME` | — | `event_study_v1` | ソース識別子 (run_id の prefix) |
| `EVENT_STUDY_PANEL_KIND` | — | `abnormal_return` | パネル種別 |
| `EVENT_STUDY_BATCH_LABEL` | — | `batch_default` | バッチラベル (run_id の suffix) |
| `EVENT_STUDY_TRACE_NAMESPACE` | — | `event_study` | trace_id 生成用 namespace |
| `EVENT_STUDY_WRITEBACK_ENABLED` | — | `true` | writeback 有効化フラグ |
| `EVENT_STUDY_WRITEBACK_DRY_RUN` | — | `false` | `true` で DB 書き込みをスキップ |
| `EVENT_STUDY_TARGET_RULES_ENABLED` | — | `true` | target 自動解決の有効化 |
| `EVENT_STUDY_AUDIT_ENABLED` | — | `true` | audit_events 書き込みの有効化 |
| `AUDIT_EVENTS_SCHEMA` | — | `public` | audit_events のスキーマ名 |
| `AUDIT_EVENTS_TABLE` | — | `audit_events` | audit_events のテーブル名 |
| `QED_CANDIDATES_SCHEMA` | — | `public` | factor_candidates のスキーマ |
| `QED_CANDIDATES_TABLE` | — | `factor_candidates` | factor_candidates のテーブル名 |
| `QED_CANDIDATES_CODE_COLUMN` | — | `name` | コード照合に使うカラム名 |

**注意**: `run_id = {EVENT_STUDY_SOURCE_NAME}__{EVENT_STUDY_BATCH_LABEL}` として構成されます。

---

## 4. Phase 別実行手順

### Phase A — DuckDB Panel → PostgreSQL Writeback

```bash
cd /home/user/prostock   # リポジトリルート
source config/env/.env.local   # 環境変数読み込み

PYTHONPATH=analytics/python python3 scripts/postgres/run_event_study_writeback.py
```

**出力例**:
```
[INFO] EventStudyWriter init: run_id=event_study_v1__batch_20260413 trace_id=93eb4b34-...
[INFO] upsert_run OK: run_id=event_study_v1__batch_20260413 total_events=0
[INFO] [DuckDB] parquet not found, using sample panel (48 rows)
[INFO] upsert_summaries OK: run_id=event_study_v1__batch_20260413 rows=48
RUN_ID=event_study_v1__batch_20260413
TRACE_ID=93eb4b34-b205-57c0-9803-085b70e0329f
```

**DRY_RUN で実行する場合**:
```bash
EVENT_STUDY_WRITEBACK_DRY_RUN=true \
  PYTHONPATH=analytics/python python3 scripts/postgres/run_event_study_writeback.py
```

### Phase B — experiment_report Bridge

```python
from pg_io.postgres_conn import get_connection
from pg_io.postgres_audit_event_writer import AuditEventWriter
from pg_io.postgres_event_study_experiment_report_bridge import ExperimentReportBridge

run_id = "event_study_v1__batch_20260413"
with get_connection() as conn:
    audit = AuditEventWriter(conn, strict=False)
    bridge = ExperimentReportBridge(conn, audit, dry_run=False)
    result = bridge.promote(run_id)
    conn.commit()
    print(result)
```

### Phase C — knowledge_artifact Bridge

```python
from pg_io.postgres_event_study_knowledge_artifact_bridge import KnowledgeArtifactBridge

with get_connection() as conn:
    audit = AuditEventWriter(conn, strict=False)
    bridge = KnowledgeArtifactBridge(conn, audit, dry_run=False)
    result = bridge.promote(run_id)
    conn.commit()
    artifact_id = result["artifact_id"]
```

### Phase D — Target Auto-Resolution

```python
from pg_io.postgres_event_study_target_rule_resolver import TargetRuleResolver

with get_connection() as conn:
    audit = AuditEventWriter(conn, strict=False)
    resolver = TargetRuleResolver(conn, audit, dry_run=False)
    result = resolver.resolve(artifact_id)
    conn.commit()
    print(result["resolution_status"])   # resolved / unresolved / ambiguous
```

**metadata に candidate_code を設定して resolved にする場合**:
```sql
UPDATE knowledge_artifacts
SET metadata = metadata || '{"candidate_code": "12M モメンタム"}'::jsonb
WHERE artifact_id = '<artifact_id>';
```

### Phase E — artifact_links Bridge

```python
from pg_io.postgres_event_study_artifact_links_bridge import ArtifactLinksBridge

with get_connection() as conn:
    audit = AuditEventWriter(conn, strict=False)
    bridge = ArtifactLinksBridge(conn, audit, dry_run=False)
    result = bridge.create_links(artifact_id)
    conn.commit()
```

---

## 5. 統合実行（bootstrap master）

全 Phase を順番に一括実行します。

```bash
cd /home/user/prostock

# 通常実行
make event-bootstrap-master

# または直接
bash scripts/postgres/run_event_study_bootstrap_master.sh

# DRY_RUN モード（DB 書き込みなし）
make event-bootstrap-master-dry
# または
bash scripts/postgres/run_event_study_bootstrap_master.sh --dry-run
```

**成功時のログ例**:
```
[10:30:01] === Step 0: DB 接続確認 ===
[10:30:01] === Step 1: Phase A — writeback ===
RUN_ID=event_study_v1__batch_20260413
TRACE_ID=93eb4b34-b205-57c0-9803-085b70e0329f
[10:30:02] === Step 2: Phase B — experiment_report bridge ===
[experiment_report] {'promotion_status': 'applied', ...}
[10:30:02] === Step 3: Phase C — knowledge_artifact bridge ===
ARTIFACT_ID=5f9ed22b-b6f4-51a6-86ed-575d93fbd295
[10:30:02] === Step 4: Phase D — target resolution ===
[target_resolution] status=resolved target=77777777-...
[10:30:02] === Step 5: Phase E — artifact_links bridge ===
[artifact_link] {'status': 'created', ...}
[10:30:02] === Step 6: Phase F — audit summary ===
=== AUDIT SUMMARY ===
{"total_pipeline_events": 6, ...}
[10:30:02] === bootstrap_master 完了 ===
  run_id    : event_study_v1__batch_20260413
  trace_id  : 93eb4b34-b205-57c0-9803-085b70e0329f
  artifact  : 5f9ed22b-b6f4-51a6-86ed-575d93fbd295
```

---

## 6. 検証コマンド

### テーブル件数の確認

```bash
# 自動検証スクリプト
bash scripts/postgres/verify_event_study_bootstrap_master.sh

# または make
make event-bootstrap-verify
```

### 個別テーブル確認

```sql
-- Phase A
SELECT run_id, status, total_events FROM event_study_summary_runs ORDER BY created_at DESC LIMIT 5;
SELECT COUNT(*), run_id FROM event_study_summaries GROUP BY run_id ORDER BY 2 DESC;

-- Phase B
SELECT run_id, promotion_status, report_title FROM event_study_experiment_report_bridge ORDER BY created_at DESC LIMIT 5;

-- Phase C
SELECT artifact_id, artifact_tag, status FROM knowledge_artifacts ORDER BY created_at DESC LIMIT 5;

-- Phase D
SELECT artifact_id, resolution_status, matched_rule_name, matched_target_type FROM target_resolution_log ORDER BY resolved_at DESC LIMIT 5;

-- Phase E
SELECT artifact_id, target_type, target_id FROM artifact_links ORDER BY created_at DESC LIMIT 5;

-- Phase F: audit_events
SELECT phase, decision, COUNT(*) FROM event_study_pipeline_audit GROUP BY phase, decision ORDER BY 1, 2;
```

### trace_id でのクロスチェック

```sql
-- 特定 trace_id の全監査ログを確認
SELECT phase, event_type, decision, created_at
FROM event_study_pipeline_audit
WHERE trace_id = '93eb4b34-b205-57c0-9803-085b70e0329f'
ORDER BY created_at;
```

---

## 7. テスト実行

```bash
cd /home/user/prostock

# ユニットテストのみ（DB 不要）
PYTHONPATH=analytics/python python -m pytest tests/unit/ -v

# 統合テスト（PostgreSQL 必要）
QED_PG_DSN="postgresql://postgres:postgres@localhost:5432/qed_dev" \
  PYTHONPATH=analytics/python \
  python -m pytest tests/integration/ -v

# 全テスト
QED_PG_DSN="postgresql://postgres:postgres@localhost:5432/qed_dev" \
  PYTHONPATH=analytics/python \
  python -m pytest tests/ -v

# make 経由
make test
```

**期待結果**:
- ユニットテスト: **55 passed**
- 統合テスト: **15 passed**
- 合計: **70 passed**

---

## 8. トラブルシューティング

### `ModuleNotFoundError: No module named 'pg_io'`

```bash
# PYTHONPATH が正しく設定されているか確認
export PYTHONPATH=/home/user/prostock/analytics/python
python3 -c "from pg_io.postgres_conn import get_connection; print('OK')"
```

### `ModuleNotFoundError: No module named 'duckdb'`

DuckDB がインストールされていない場合、パイプラインは自動的にサンプルデータ（48行）にフォールバックします。実 Parquet を使用するには：

```bash
pip install duckdb
# EVENT_AR_OUTPUT_PATH に Parquet ファイルパスを設定
export EVENT_AR_OUTPUT_PATH=/path/to/ar_panel.parquet
```

### `psycopg.errors.UndefinedTable`

マイグレーションが未適用です：

```bash
bash scripts/postgres/init_event_study_tables.sh
bash scripts/postgres/init_event_study_experiment_report_bridge.sh
bash scripts/postgres/init_event_study_knowledge_artifact_bridge.sh
bash scripts/postgres/init_event_study_target_rule_auto_resolution.sh
bash scripts/postgres/init_event_study_audit_events_integration.sh
```

### Phase D で常に `unresolved` になる

`factor_candidates` に対応するレコードが存在しないか、`candidate_code` が metadata に設定されていません：

```sql
-- 利用可能な factor_candidates を確認
SELECT id, name, status FROM factor_candidates WHERE status != 'deprecated';

-- metadata に candidate_code を設定
UPDATE knowledge_artifacts
SET metadata = metadata || '{"candidate_code": "12M モメンタム"}'::jsonb
WHERE artifact_id = '<artifact_id>';
```

### bootstrap_master が `run_id を取得できません` で失敗

Phase A の出力に `RUN_ID=` が含まれていない場合：

```bash
# 手動で Phase A を実行してログを確認
PYTHONPATH=analytics/python python3 scripts/postgres/run_event_study_writeback.py 2>&1 | tee /tmp/debug.log
grep "RUN_ID\|ERROR\|Error" /tmp/debug.log
```

---

## 9. 再実行・べき等性

パイプライン全体は **UPSERT** ベースで設計されており、同じ `run_id` / `batch_label` で再実行してもデータが重複しません。

```bash
# 同じ batch_label で 2 回実行しても安全
EVENT_STUDY_BATCH_LABEL=batch_20260413 bash scripts/postgres/run_event_study_bootstrap_master.sh
EVENT_STUDY_BATCH_LABEL=batch_20260413 bash scripts/postgres/run_event_study_bootstrap_master.sh

# 件数が変わらないことを確認
psql "$QED_PG_DSN" -c "SELECT COUNT(*) FROM event_study_summary_runs WHERE run_id LIKE '%batch_20260413';"
# → 1 (重複なし)
```

**UPSERT の競合キー**:
| テーブル | ON CONFLICT キー |
|---------|----------------|
| `event_study_summary_runs` | `run_id` |
| `event_study_summaries` | `(run_id, benchmark_id, event_offset)` |
| `event_study_experiment_report_bridge` | `run_id` |
| `knowledge_artifacts` | `artifact_id` |
| `target_resolution_log` | `artifact_id` |
| `artifact_links` | `(artifact_id, target_id, link_type)` |

---

## 10. 既知の制約と拡張予定

### 現在の制約

| 制約 | 内容 |
|------|------|
| DuckDB 非インストール時 | サンプルデータ（48行）でフォールバック。実環境では `pip install duckdb` が必要 |
| target_resolution P5 alias fallback | 未実装。現在 P1〜P4 のみ対応 |
| ambiguous 時の自動選択 | ambiguous は REJECTED として記録され、手動解決が必要 |
| audit_events の `trace_id` 型 | QED 本体は `text`、UUID5 文字列で格納 |
| bootstrap_master のロールバック | 中断時の部分実行がロールバックされない（各 Phase は独立 commit） |

### 拡張予定

- **P5 alias fallback**: `target_rule_aliases` テーブルを用いた柔軟なコードマッピング
- **DuckDB 実 Parquet 対応**: `EVENT_AR_OUTPUT_PATH` 経由の本番 Parquet 読み込み
- **ambiguous 自動解決**: スコアリングや優先度に基づく自動選択ロジック
- **CI/CD 統合**: GitHub Actions での自動テスト・デプロイ
- **監視アラート**: audit_events の REJECTED 件数が閾値超過時に通知
