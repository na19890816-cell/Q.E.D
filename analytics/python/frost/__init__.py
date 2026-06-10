"""
FROST Meta-Fitness Engine パッケージ。

FROST は ProStock / Q.E.D. の Phase 5 research/selection layer として機能する。
EML Alpha Discovery・event study・既存 alpha engine から得た候補を評価し、
SELECTED / HOLD / REJECTED / REVIEW_REQUIRED を決定する。

公開 API:
  - FrostConfig / load_frost_config()
  - FrostCandidate / FrostEvaluation / FrostDecision / FrostRunOutput
  - run_frost_pipeline()
  - frost_candidates_from_eml()
  - build_markdown_report() / build_json_summary()
"""
from analytics.python.frost.frost_config import FrostConfig, load_frost_config
from analytics.python.frost.frost_contracts import (
    FrostAuditRecord,
    FrostCandidate,
    FrostDecision,
    FrostEvaluation,
    FrostPromotionRecord,
    FrostRunOutput,
)
from analytics.python.frost.frost_runner import (
    frost_candidates_from_eml,
    run_frost_pipeline,
)
from analytics.python.frost.frost_report_builder import (
    analyze_rejections,
    build_json_summary,
    build_markdown_report,
)

__all__ = [
    # Config
    "FrostConfig",
    "load_frost_config",
    # Contracts
    "FrostCandidate",
    "FrostEvaluation",
    "FrostDecision",
    "FrostRunOutput",
    "FrostAuditRecord",
    "FrostPromotionRecord",
    # Runner
    "run_frost_pipeline",
    "frost_candidates_from_eml",
    # Report
    "build_markdown_report",
    "build_json_summary",
    "analyze_rejections",
]
