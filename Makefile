# =============================================================================
# Makefile — Event Study Pipeline
# =============================================================================
SHELL       := /bin/bash
REPO_ROOT   := $(shell pwd)
ENV_FILE    := $(REPO_ROOT)/config/env/.env.local
PYPATH      := $(REPO_ROOT)/analytics/python
PYTHON      := python3
PG_DSN      ?= postgresql://postgres:postgres@localhost:5432/qed_dev

# .env.local が存在すれば自動ロード
ifneq ($(wildcard $(ENV_FILE)),)
  include $(ENV_FILE)
  export
endif

.PHONY: help setup \
        event-study-db-init event-report-bridge-init event-ka-bridge-init \
        event-target-rules-init event-audit-init \
        event-ar-panel event-study-writeback event-report-bridge \
        event-ka-bridge event-target-rules-resolve event-artifact-links-bridge \
        event-bootstrap-master event-bootstrap-verify event-audit-status \
        eml-terminal eml-search eml-evaluate eml-backtest \
        eml-backtest-summary eml-promote eml-status eml-pipeline \
        lint test

# ---------------------------------------------------------------------------
help: ## このヘルプを表示
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-38s\033[0m %s\n", $$1, $$2}'

setup: ## 初期セットアップ (.env.local 生成)
	@if [ ! -f $(ENV_FILE) ]; then \
	  cp config/env/.env.example $(ENV_FILE); \
	  echo "[setup] $(ENV_FILE) を作成しました。QED_PG_DSN 等を設定してください。"; \
	else \
	  echo "[setup] $(ENV_FILE) は既に存在します。"; \
	fi
	@pip install psycopg[binary] pandas numpy 2>/dev/null || true

# ---------------------------------------------------------------------------
# DB 初期化 (migration)
# ---------------------------------------------------------------------------
event-study-db-init: ## migration 015: event_study_summary_runs/summaries
	bash scripts/postgres/init_event_study_tables.sh

event-report-bridge-init: ## migration 016: experiment_report_bridge
	bash scripts/postgres/init_event_study_experiment_report_bridge.sh

event-ka-bridge-init: ## migration 017+018: knowledge_artifacts + artifact_links
	bash scripts/postgres/init_event_study_knowledge_artifact_bridge.sh

event-target-rules-init: ## migration 020: target_rule_auto_resolution
	bash scripts/postgres/init_event_study_target_rule_auto_resolution.sh

event-audit-init: ## migration 021: audit_events_integration
	bash scripts/postgres/init_event_study_audit_events_integration.sh

# ---------------------------------------------------------------------------
# パイプライン実行
# ---------------------------------------------------------------------------
event-ar-panel: ## DuckDB で AR panel を生成 (テスト用サンプル)
	PYTHONPATH=$(PYPATH) $(PYTHON) analytics/python/features/build_event_study_abnormal_return_panel.py

event-study-writeback: ## Phase A: DuckDB panel → PostgreSQL writeback
	PYTHONPATH=$(PYPATH) QED_PG_DSN=$(PG_DSN) \
	  $(PYTHON) scripts/postgres/run_event_study_writeback.py

event-report-bridge: ## Phase B: summary → experiment_reports
	@echo "Phase B: run_event_study_bootstrap_master.sh の Step 2 を参照"

event-ka-bridge: ## Phase C: experiment_reports → knowledge_artifacts
	@echo "Phase C: run_event_study_bootstrap_master.sh の Step 3 を参照"

event-target-rules-resolve: ## Phase D: target auto resolution
	@echo "Phase D: run_event_study_bootstrap_master.sh の Step 4 を参照"

event-artifact-links-bridge: ## Phase E: artifact_links 生成
	@echo "Phase E: run_event_study_bootstrap_master.sh の Step 5 を参照"

# ---------------------------------------------------------------------------
# 統合実行
# ---------------------------------------------------------------------------
event-bootstrap-master: ## 全 Phase を一括実行
	QED_PG_DSN=$(PG_DSN) bash scripts/postgres/run_event_study_bootstrap_master.sh

event-bootstrap-master-dry: ## 全 Phase を DRY_RUN で実行
	QED_PG_DSN=$(PG_DSN) bash scripts/postgres/run_event_study_bootstrap_master.sh --dry-run

event-bootstrap-verify: ## 全テーブル件数を検証
	bash scripts/postgres/verify_event_study_bootstrap_master.sh

event-audit-status: ## 監査ログサマリーを表示
	psql $(PG_DSN) -c "SELECT phase, decision, COUNT(*) FROM event_study_pipeline_audit GROUP BY phase, decision ORDER BY 1,2;"

# ---------------------------------------------------------------------------
# EML Alpha Discovery Pipeline
# ---------------------------------------------------------------------------

eml-terminal: ## EML Phase A: ターミナルセット構築・確認
	PYTHONPATH=$(REPO_ROOT) python3 -c "\
import sys; sys.path.insert(0,'$(REPO_ROOT)'); \
from analytics.python.features.build_terminal_set import get_terminal_set_from_env; \
ts = get_terminal_set_from_env(); \
print('Terminal set (%d):' % len(ts)); [print(' -', t) for t in ts]"

eml-search: ## EML Phase B: 探索実行 (EML_ALPHA_ENABLED=1 必須)
	PYTHONPATH=$(REPO_ROOT) \
	QED_PG_DSN=$(PG_DSN) \
	EML_ALPHA_ENABLED=1 \
	  python3 scripts/postgres/run_eml_pipeline.py 2>&1 | head -80

eml-evaluate: ## EML Phase C: 評価実行 (search 内包)
	@echo "evaluate は run_eml_pipeline.py (eml-search) に内包されています"
	@echo "make eml-search を実行してください"

eml-backtest: ## EML Phase D: バックテスト実行 (search 内包)
	@echo "backtest は run_eml_pipeline.py (eml-search) に内包されています"
	@echo "make eml-search を実行してください"

eml-backtest-summary: ## EML バックテスト結果サマリー表示
	psql $(PG_DSN) -c "\
SELECT left(br.backtest_run_id,8) AS run_short, br.mode, br.total_folds, \
       round((br.summary_json->>'overall_sharpe')::numeric, 4) AS sharpe, \
       round((br.summary_json->>'overall_max_drawdown')::numeric, 4) AS mdd, \
       br.status, br.created_at::date AS dt \
FROM eml_backtest_runs br \
ORDER BY br.created_at DESC LIMIT 10;"

eml-promote: ## EML Phase E: Q.E.D.チェーンへプロモーション
	PYTHONPATH=$(REPO_ROOT) \
	QED_PG_DSN=$(PG_DSN) \
	EML_ALPHA_ENABLED=1 \
	  python3 scripts/postgres/run_eml_pipeline.py

eml-pipeline: ## EML フルパイプライン実行 (A→B→C→D→E)
	PYTHONPATH=$(REPO_ROOT) \
	QED_PG_DSN=$(PG_DSN) \
	EML_ALPHA_ENABLED=1 \
	  python3 scripts/postgres/run_eml_pipeline.py

eml-pipeline-dry: ## EML フルパイプライン DRY_RUN
	PYTHONPATH=$(REPO_ROOT) \
	QED_PG_DSN=$(PG_DSN) \
	EML_ALPHA_ENABLED=1 \
	EML_ALPHA_DRY_RUN=1 \
	  python3 scripts/postgres/run_eml_pipeline.py

eml-status: ## EML テーブル件数・ステータス確認
	psql $(PG_DSN) -c "\
SELECT 'eml_alpha_runs'        AS tbl, COUNT(*) FROM eml_alpha_runs UNION ALL \
SELECT 'eml_alpha_candidates'  AS tbl, COUNT(*) FROM eml_alpha_candidates UNION ALL \
SELECT 'eml_alpha_evaluations' AS tbl, COUNT(*) FROM eml_alpha_evaluations UNION ALL \
SELECT 'eml_alpha_promotion_bridge' AS tbl, COUNT(*) FROM eml_alpha_promotion_bridge UNION ALL \
SELECT 'eml_backtest_runs'     AS tbl, COUNT(*) FROM eml_backtest_runs UNION ALL \
SELECT 'eml_backtest_folds'    AS tbl, COUNT(*) FROM eml_backtest_folds ORDER BY 1;"
	psql $(PG_DSN) -c "\
SELECT decision, COUNT(*) \
FROM audit_events WHERE event_type LIKE 'EML_%' \
GROUP BY decision ORDER BY 1;"

# ---------------------------------------------------------------------------
# FROST Meta-Fitness Engine
# ---------------------------------------------------------------------------

frost-init: ## FROST テーブル・ビューを初期化する
	QED_PG_DSN=$(PG_DSN) bash scripts/frost/init_frost_tables.sh

frost-init-dry: ## FROST 初期化 DRY_RUN (SQL 表示のみ)
	QED_PG_DSN=$(PG_DSN) bash scripts/frost/init_frost_tables.sh --dry-run

frost-pipeline: ## FROST フルパイプライン実行
	PYTHONPATH=$(REPO_ROOT) \
	QED_PG_DSN=$(PG_DSN) \
	FROST_ENABLED=1 \
	FROST_DRY_RUN=0 \
	  bash scripts/frost/run_frost_engine.sh

frost-pipeline-dry: ## FROST フルパイプライン DRY_RUN
	PYTHONPATH=$(REPO_ROOT) \
	QED_PG_DSN=$(PG_DSN) \
	FROST_ENABLED=1 \
	FROST_DRY_RUN=1 \
	  bash scripts/frost/run_frost_engine.sh --dry-run

frost-backfill: ## FROST 過去候補の遡及評価
	PYTHONPATH=$(REPO_ROOT) \
	QED_PG_DSN=$(PG_DSN) \
	  bash scripts/frost/run_frost_backfill.sh

frost-backfill-dry: ## FROST 遡及評価 DRY_RUN
	PYTHONPATH=$(REPO_ROOT) \
	QED_PG_DSN=$(PG_DSN) \
	  bash scripts/frost/run_frost_backfill.sh --dry-run

frost-promote: ## FROST SELECTED候補を Q.E.D. に昇格
	PYTHONPATH=$(REPO_ROOT) \
	QED_PG_DSN=$(PG_DSN) \
	  bash scripts/frost/run_frost_promote.sh

frost-promote-dry: ## FROST 昇格 DRY_RUN
	PYTHONPATH=$(REPO_ROOT) \
	QED_PG_DSN=$(PG_DSN) \
	  bash scripts/frost/run_frost_promote.sh --dry-run

frost-verify: ## FROST エンジン動作検証
	QED_PG_DSN=$(PG_DSN) bash scripts/frost/verify_frost_engine.sh

frost-status: ## FROST テーブル件数・ステータス確認
	psql $(PG_DSN) -c "\
SELECT 'frost_runs'                AS tbl, COUNT(*) FROM frost_runs UNION ALL \
SELECT 'frost_fitness_candidates'  AS tbl, COUNT(*) FROM frost_fitness_candidates UNION ALL \
SELECT 'frost_evaluations'         AS tbl, COUNT(*) FROM frost_evaluations UNION ALL \
SELECT 'frost_selection_decisions' AS tbl, COUNT(*) FROM frost_selection_decisions UNION ALL \
SELECT 'frost_promotion_bridges'   AS tbl, COUNT(*) FROM frost_promotion_bridges UNION ALL \
SELECT 'frost_audit_event_bridges' AS tbl, COUNT(*) FROM frost_audit_event_bridges ORDER BY 1;"
	psql $(PG_DSN) -c "\
SELECT decision, COUNT(*) \
FROM frost_selection_decisions \
GROUP BY decision ORDER BY 1;"

frost-clean: ## FROST テーブルの全データを削除 (危険: 本番禁止)
	@echo "WARNING: frost テーブルの全データを削除します"
	@read -p "本当に実行しますか? [y/N]: " ans; \
	if [ "$$ans" = "y" ]; then \
	  psql $(PG_DSN) -c "TRUNCATE frost_audit_event_bridges, frost_promotion_bridges, frost_selection_decisions, frost_evaluations, frost_fitness_candidates, frost_runs CASCADE"; \
	  echo "削除完了"; \
	else \
	  echo "キャンセルしました"; \
	fi

# ---------------------------------------------------------------------------
test: ## Python ユニットテスト実行
	PYTHONPATH=$(PYPATH) $(PYTHON) -m pytest tests/ -v 2>/dev/null || \
	  PYTHONPATH=$(PYPATH) $(PYTHON) -m pytest analytics/python/ -v 2>/dev/null || \
	  echo "[test] pytest 未インストール or テストなし"

include Makefile.golden
