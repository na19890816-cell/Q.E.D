# Event Study Pipeline — Handover / README

**プロジェクト**: prostock-bff-v2 / Q.E.D. Event Study パイプライン  
**ステータス**: ✅ 実装完了・テスト全通過  
**最終更新**: 2026-04-13

---

## プロジェクト概要

DuckDB が生成したイベントスタディ（異常収益 / CAR）パネルデータを  
Q.E.D. PostgreSQL 監査レイヤーへ自動連携するパイプラインです。

### アーキテクチャ

```
DuckDB / Parquet
      │
      ▼  Phase A — Writeback
  [event_study_summary_runs]
  [event_study_summaries]
      │
      ▼  Phase B — experiment_report bridge
  [event_study_experiment_report_bridge]
  [experiment_runs]  (QED本体)
      │
      ▼  Phase C — knowledge_artifact bridge
  [knowledge_artifacts]
      │
      ▼  Phase D — target auto-resolution
  [target_resolution_log]  ──→  factor_candidates / hypotheses
      │
      ▼  Phase E — artifact_links
  [artifact_links]
      │
      ▼  Phase F — audit
  [audit_events]  (QED本体)
  [event_study_pipeline_audit]
```

---

## 実装済み機能 (✅ 完了)

| Phase | 機能 | ファイル |
|-------|------|---------|
| A | DuckDB panel → PostgreSQL UPSERT | `pg_io/postgres_event_study_writer.py` |
| B | summary_runs → experiment_reports | `pg_io/postgres_event_study_experiment_report_bridge.py` |
| C | experiment_reports → knowledge_artifacts | `pg_io/postgres_event_study_knowledge_artifact_bridge.py` |
| D | candidate/hypothesis コードによる target 自動解決 | `pg_io/postgres_event_study_target_rule_resolver.py` |
| E | 解決済み target への artifact_links 生成 | `pg_io/postgres_event_study_artifact_links_bridge.py` |
| F | 全 Phase での audit_events 二重書き込み | `pg_io/postgres_audit_event_writer.py` |
| — | 全 Phase の統合実行 | `scripts/postgres/run_event_study_bootstrap_master.sh` |
| — | ユニットテスト 55件 | `tests/unit/` |
| — | 統合テスト 15件 | `tests/integration/` |

---

## ファイル構成

```
prostock/
├── analytics/
│   └── python/
│       ├── features/
│       │   └── build_event_study_abnormal_return_panel.py   # DuckDB panel builder
│       └── pg_io/
│           ├── postgres_conn.py                              # DB 接続ユーティリティ
│           ├── postgres_audit_event_writer.py               # audit_events 書き込み
│           ├── postgres_event_study_writer.py               # Phase A: writeback
│           ├── postgres_event_study_experiment_report_bridge.py  # Phase B
│           ├── postgres_event_study_knowledge_artifact_bridge.py # Phase C
│           ├── postgres_event_study_target_rule_resolver.py      # Phase D
│           ├── postgres_event_study_artifact_links_bridge.py     # Phase E
│           ├── postgres_event_study_audit_bridge.py             # Phase F: サマリ取得
│           └── postgres_artifact_link_target_catalog.py         # target rule catalog
├── config/
│   └── env/
│       └── .env.example              # 環境変数テンプレート
├── docs/
│   ├── handover.md                   # このファイル
│   └── runbooks/
│       └── event_study_pipeline_runbook.md  # 詳細運用手順
├── qedschema/
│   ├── migrations/
│   │   ├── 015_event_study_summary_tables.sql
│   │   ├── 016_event_study_experiment_report_bridge.sql
│   │   ├── 017_event_study_knowledge_artifact_bridge.sql
│   │   ├── 018_event_study_artifact_links_bridge.sql
│   │   ├── 020_event_study_target_rule_auto_resolution.sql
│   │   └── 021_event_study_audit_events_integration.sql
│   ├── seeds/
│   │   └── 020_event_study_target_rule_seed.sql
│   └── views/
│       ├── 015_v_event_study_dashboard.sql
│       ├── 016_v_event_study_experiment_reports.sql
│       ├── 017_v_event_study_knowledge_artifacts.sql
│       ├── 020_v_event_study_target_rule_resolution_status.sql
│       └── 021_v_event_study_audit_events.sql
├── scripts/
│   └── postgres/
│       ├── run_event_study_writeback.py              # Phase A エントリポイント
│       ├── run_event_study_bootstrap_master.sh       # 全 Phase 統合実行
│       ├── verify_event_study_bootstrap_master.sh    # 件数検証
│       ├── init_event_study_tables.sh
│       ├── init_event_study_experiment_report_bridge.sh
│       ├── init_event_study_knowledge_artifact_bridge.sh
│       ├── init_event_study_target_rule_auto_resolution.sh
│       └── init_event_study_audit_events_integration.sh
├── tests/
│   ├── unit/
│   │   ├── test_event_study_writer.py        # EventStudyWriter: 30件
│   │   ├── test_audit_payload.py             # AuditEventWriter: 11件
│   │   ├── test_target_rule_resolver.py      # TargetRuleResolver: 14件
│   │   └── test_trace_id_consistency.py      # trace_id一貫性: 10件
│   └── integration/
│       └── test_pipeline_integration.py      # 実DB統合テスト: 15件
├── Makefile                                  # タスク自動化
└── pytest.ini
```

---

## データモデル（追加テーブル）

### マイグレーション 015: event_study_summary_runs / event_study_summaries

```sql
-- パイプライン実行単位
event_study_summary_runs (
  run_id TEXT PRIMARY KEY,          -- "{source_name}__{batch_label}"
  trace_id TEXT NOT NULL,           -- UUID5 決定論的生成
  source_name TEXT,
  panel_kind TEXT,                  -- abnormal_return | car
  batch_label TEXT,
  status TEXT,                      -- running | completed | failed
  total_events INT,
  run_metadata JSONB,
  started_at TIMESTAMPTZ,
  completed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ
)

-- 個別イベントサマリ
event_study_summaries (
  id BIGSERIAL PRIMARY KEY,
  run_id TEXT,
  trace_id TEXT,
  benchmark_id TEXT,
  event_date DATE,
  event_offset INT,
  abnormal_return NUMERIC,
  car_from_t0 NUMERIC,
  normal_return NUMERIC,
  actual_return NUMERIC,
  n_events INT,
  extra_metrics JSONB,
  UNIQUE(run_id, benchmark_id, event_offset)
)
```

### マイグレーション 016: event_study_experiment_report_bridge

experiment_runs (QED) との接続テーブル。

### マイグレーション 017: knowledge_artifacts

```sql
knowledge_artifacts (
  artifact_id TEXT PRIMARY KEY,     -- UUID5 (trace_id + run_id から生成)
  trace_id TEXT,
  artifact_tag TEXT,                -- "event_study:{run_id}"
  artifact_type TEXT,
  title TEXT,
  summary TEXT,
  status TEXT,                      -- draft | active | archived
  metadata JSONB,                   -- candidate_code, hypothesis_code 等
  created_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ
)
```

### マイグレーション 018: artifact_links

```sql
artifact_links (
  id BIGSERIAL PRIMARY KEY,
  artifact_id TEXT,
  target_id TEXT,                   -- factor_candidates.id 等
  target_type TEXT,                 -- factor_candidate | hypothesis
  link_type TEXT DEFAULT 'evidence',
  created_at TIMESTAMPTZ,
  UNIQUE(artifact_id, target_id, link_type)
)
```

### マイグレーション 020: target_resolution_log + event_study_target_rules

target_rule_auto_resolution の実行ログとルール定義テーブル。

### マイグレーション 021: event_study_pipeline_audit

pipeline 専用の補助 audit テーブル（QED `audit_events` への書き込みが失敗した場合のフォールバック）。

---

## クイックスタート

### 1. セットアップ

```bash
cd /home/user/prostock
make setup          # .env.local 生成 + pip install
```

### 2. マイグレーション適用

```bash
# 全マイグレーション + seed + views を一括適用
bash scripts/postgres/init_event_study_tables.sh
bash scripts/postgres/init_event_study_experiment_report_bridge.sh
bash scripts/postgres/init_event_study_knowledge_artifact_bridge.sh
bash scripts/postgres/init_event_study_target_rule_auto_resolution.sh
bash scripts/postgres/init_event_study_audit_events_integration.sh
psql "$QED_PG_DSN" -f qedschema/seeds/020_event_study_target_rule_seed.sql
```

### 3. パイプライン実行

```bash
# 全 Phase を一括実行
make event-bootstrap-master

# DRY_RUN（DB 書き込みなし）
make event-bootstrap-master-dry
```

### 4. テスト実行

```bash
make test
# ユニット 55件 + 統合 15件 = 70 passed
```

---

## trace_id の設計

`trace_id` は以下の式で **決定論的** に生成されます：

```python
trace_id = UUID5(UUID5(DNS, namespace), run_id)
# 例: namespace="event_study", run_id="event_study_v1__batch_20260413"
# → "93eb4b34-b205-57c0-9803-085b70e0329f"
```

- 同じ `source_name` + `batch_label` なら常に同じ `trace_id`
- 全ステージ（Phase A〜F）で同一の `trace_id` を伝播
- `audit_events` / `event_study_pipeline_audit` のクロス検索に使用可能

---

## target 自動解決の優先順位

| 優先度 | ルール名 | 解決方法 |
|--------|---------|---------|
| P1 | `candidate_code_direct` | `metadata.candidate_code` → `factor_candidates.name` |
| P2 | `hypothesis_code_direct` | `metadata.hypothesis_code` → `hypotheses.title` |
| P3 | `tag_candidate` | `artifact_tag = "candidate:{code}"` → factor_candidates |
| P4 | `tag_hypothesis` | `artifact_tag = "hypothesis:{code}"` → hypotheses |
| P5 | alias fallback | 未実装（拡張予定） |

**解決結果**:
- `resolved`: 1件一致 → `artifact_links` 生成、audit `APPLIED`
- `ambiguous`: 複数件一致 → audit `REJECTED` (TARGET_AMBIGUOUS)
- `unresolved`: 0件一致 → audit `REJECTED` (TARGET_UNRESOLVED)

---

## 受け入れ基準 (Acceptance Criteria) 達成状況

| # | 基準 | 状態 |
|---|------|------|
| 1 | panel parquet を summary テーブルへ writeback | ✅ |
| 2 | run_id から experiment_reports を生成 | ✅ |
| 3 | knowledge_artifacts を生成 | ✅ |
| 4 | candidate/hypothesis の少なくとも 1 件を自動解決 | ✅ |
| 5 | 解決済み target へ artifact_links を作成 | ✅ |
| 6 | 各ステップで audit_events を記録 | ✅ |
| 7 | UPSERT により再実行で重複しない | ✅ |
| 8 | unresolved/ambiguous を記録 | ✅ |

---

## 未実装・スコープ外

- UI / 可視化ダッシュボード
- 追加金融モデル
- QED publish API との深い連携
- moomoo 実行連携
- DDL トリガー自動化
- target alias fallback (P5) の実装
- ambiguous 時の自動選択ロジック

---

## 関連ドキュメント

- **詳細運用手順**: `docs/runbooks/event_study_pipeline_runbook.md`
- **環境変数テンプレート**: `config/env/.env.example`
- **マイグレーション SQL**: `qedschema/migrations/`
