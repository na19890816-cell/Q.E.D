"""
frost_report_builder.py
-----------------------
FROST レビュー用レポート生成モジュール。

FrostRunOutput から:
1. Markdown 形式のレビューレポートを生成
2. JSON 形式のサマリーを生成
3. rejection analysis (why was this rejected?) を出力

設計原則:
  - 副作用なし (ファイル書き込みは行わない、文字列を返す)
  - テスト容易性を重視
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from analytics.python.frost.frost_contracts import (
    FrostCandidate,
    FrostDecision,
    FrostEvaluation,
    FrostRunOutput,
)


# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------

def _fmt_float(v: Any, decimals: int = 4) -> str:
    if v is None:
        return "N/A"
    try:
        return f"{float(v):.{decimals}f}"
    except (TypeError, ValueError):
        return "N/A"


def _fmt_bool(v: bool) -> str:
    return "✅ PASS" if v else "❌ FAIL"


def _decision_emoji(decision: str) -> str:
    return {
        "SELECTED":        "🟢",
        "HOLD":            "🟡",
        "REJECTED":        "🔴",
        "REVIEW_REQUIRED": "🔵",
    }.get(decision, "⚪")


# ---------------------------------------------------------------------------
# Markdown レポート生成
# ---------------------------------------------------------------------------

def build_markdown_report(output: FrostRunOutput) -> str:
    """
    FrostRunOutput から Markdown 形式のレビューレポートを生成する。

    Returns
    -------
    str
        Markdown テキスト
    """
    lines = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    # ヘッダー
    lines += [
        f"# FROST Meta-Fitness Engine レポート",
        f"",
        f"**生成日時:** {now}  ",
        f"**run_id:** `{output.run_id}`  ",
        f"**trace_id:** `{output.trace_id}`  ",
        f"**batch_label:** `{output.batch_label}`  ",
        f"**engine_version:** `{output.engine_version}`  ",
        f"**dry_run:** `{output.dry_run}`  ",
        f"**status:** `{output.status}`  ",
        f"",
    ]

    # 実行サマリー
    lines += [
        "## 実行サマリー",
        "",
        f"| 指標 | 件数 |",
        f"|------|------|",
        f"| 候補数 | {output.candidate_count} |",
        f"| 評価済み | {output.evaluated_count} |",
        f"| 🟢 SELECTED | {output.selected_count} |",
        f"| 🟡 HOLD | {output.hold_count} |",
        f"| 🔴 REJECTED | {output.rejected_count} |",
        f"| 🔵 REVIEW_REQUIRED | {sum(1 for d in output.decisions if d.decision == 'REVIEW_REQUIRED')} |",
        f"| 昇格適格 | {output.promotion_count} |",
        f"",
    ]

    # 実行時間
    if output.ended_at and output.started_at:
        elapsed = (output.ended_at - output.started_at).total_seconds()
        lines.append(f"**実行時間:** {elapsed:.1f} 秒\n")

    if output.error_message:
        lines += [
            "## ⚠️ エラー",
            "",
            f"```",
            output.error_message,
            "```",
            "",
        ]

    # 評価を dict 化
    eval_by_cid: Dict[str, FrostEvaluation] = {
        ev.candidate_id: ev for ev in output.evaluations
    }
    cand_by_cid: Dict[str, FrostCandidate] = {
        c.candidate_id: c for c in output.candidates
    }

    # SELECTED 候補テーブル
    selected = sorted(
        [d for d in output.decisions if d.decision == "SELECTED"],
        key=lambda d: (d.decision_rank is None, d.decision_rank or 999),
    )
    if selected:
        lines += [
            "## 🟢 採択候補",
            "",
            "| Rank | candidate_id | frost_score | OOS Sharpe | Rank IC | PBO | Hard Gate | 昇格 |",
            "|------|-------------|-------------|-----------|---------|-----|-----------|------|",
        ]
        for d in selected:
            ev = eval_by_cid.get(d.candidate_id)
            rank     = d.decision_rank or "-"
            fs       = _fmt_float(d.frost_score, 6)
            sharpe   = _fmt_float(ev.oos_sharpe if ev else None)
            rank_ic  = _fmt_float(ev.rank_ic if ev else None)
            pbo      = _fmt_float(ev.pbo_score if ev else None)
            gate     = _fmt_bool(ev.hard_gate_passed if ev else True)
            promo    = "✅" if d.promotion_eligible else "—"
            cid_short = d.candidate_id[:8]
            lines.append(
                f"| {rank} | `{cid_short}...` | {fs} | {sharpe} | {rank_ic} | {pbo} | {gate} | {promo} |"
            )
        lines.append("")

    # REVIEW_REQUIRED 候補
    review_req = [d for d in output.decisions if d.decision == "REVIEW_REQUIRED"]
    if review_req:
        lines += [
            "## 🔵 レビュー要請候補",
            "",
            "| candidate_id | frost_score | 理由 |",
            "|-------------|-------------|------|",
        ]
        for d in sorted(review_req, key=lambda x: x.frost_score, reverse=True):
            cid_short = d.candidate_id[:8]
            reason    = d.decision_reason[:60].replace("|", "\\|")
            lines.append(f"| `{cid_short}...` | {_fmt_float(d.frost_score, 6)} | {reason} |")
        lines.append("")

    # REJECTED サマリー
    rejected = [d for d in output.decisions if d.decision == "REJECTED"]
    if rejected:
        lines += [
            "## 🔴 棄却候補サマリー",
            "",
            f"棄却件数: **{len(rejected)}**",
            "",
        ]
        # Gate 失敗内訳
        gate_counts: Dict[str, int] = {}
        dedup_count = 0
        for d in rejected:
            if d.suppressed_by_dedup:
                dedup_count += 1
            for gf in d.gate_failures:
                # gate 名を先頭から抽出
                gate_name = gf.split("=")[0].strip()
                gate_counts[gate_name] = gate_counts.get(gate_name, 0) + 1

        if gate_counts:
            lines += [
                "### Gate 失敗内訳",
                "",
                "| Gate | 件数 |",
                "|------|------|",
            ]
            for gate, cnt in sorted(gate_counts.items(), key=lambda x: -x[1]):
                lines.append(f"| {gate} | {cnt} |")
            lines.append("")

        if dedup_count > 0:
            lines.append(f"- Near-duplicate 抑制: **{dedup_count}** 件\n")

    # 設定スナップショット
    if output.config_snapshot:
        lines += [
            "## 設定スナップショット",
            "",
            "```json",
            json.dumps(output.config_snapshot, ensure_ascii=False, indent=2),
            "```",
            "",
        ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# JSON サマリー生成
# ---------------------------------------------------------------------------

def build_json_summary(output: FrostRunOutput) -> Dict[str, Any]:
    """
    FrostRunOutput から JSON シリアライズ可能なサマリー辞書を生成する。

    Returns
    -------
    dict
    """
    eval_by_cid = {ev.candidate_id: ev for ev in output.evaluations}

    selected_details = []
    for d in sorted(
        [x for x in output.decisions if x.decision == "SELECTED"],
        key=lambda x: x.frost_score,
        reverse=True,
    ):
        ev = eval_by_cid.get(d.candidate_id)
        selected_details.append({
            "candidate_id":     d.candidate_id,
            "decision_rank":    d.decision_rank,
            "frost_score":      d.frost_score,
            "oos_sharpe":       ev.oos_sharpe if ev else None,
            "rank_ic":          ev.rank_ic if ev else None,
            "pbo_score":        ev.pbo_score if ev else None,
            "hard_gate_passed": ev.hard_gate_passed if ev else True,
            "promotion_eligible": d.promotion_eligible,
            "review_required":  d.review_required,
        })

    rejection_breakdown: Dict[str, int] = {}
    for d in output.decisions:
        if d.decision == "REJECTED":
            for gf in d.gate_failures:
                gate_name = gf.split("=")[0].strip()
                rejection_breakdown[gate_name] = rejection_breakdown.get(gate_name, 0) + 1

    return {
        "run_id":          output.run_id,
        "trace_id":        output.trace_id,
        "batch_label":     output.batch_label,
        "engine_version":  output.engine_version,
        "status":          output.status,
        "dry_run":         output.dry_run,
        "started_at":      output.started_at.isoformat() if output.started_at else None,
        "ended_at":        output.ended_at.isoformat() if output.ended_at else None,
        "counts": {
            "candidates":         output.candidate_count,
            "evaluated":          output.evaluated_count,
            "selected":           output.selected_count,
            "hold":               output.hold_count,
            "rejected":           output.rejected_count,
            "review_required":    sum(1 for d in output.decisions if d.decision == "REVIEW_REQUIRED"),
            "promotion_eligible": output.promotion_count,
            "dedup_suppressed":   sum(1 for d in output.decisions if d.suppressed_by_dedup),
        },
        "selected_candidates":   selected_details,
        "rejection_breakdown":   rejection_breakdown,
        "config_snapshot":       output.config_snapshot,
        "error_message":         output.error_message,
    }


# ---------------------------------------------------------------------------
# rejection analysis
# ---------------------------------------------------------------------------

def analyze_rejections(output: FrostRunOutput) -> str:
    """
    棄却候補の詳細分析レポートを生成する。

    Returns
    -------
    str
        Markdown テキスト
    """
    rejected = [d for d in output.decisions if d.decision == "REJECTED"]
    eval_by_cid = {ev.candidate_id: ev for ev in output.evaluations}

    lines = [
        "# FROST 棄却分析レポート",
        "",
        f"**run_id:** `{output.run_id}`",
        f"**棄却候補数:** {len(rejected)}",
        "",
    ]

    if not rejected:
        lines.append("棄却候補なし。\n")
        return "\n".join(lines)

    lines += [
        "## 棄却詳細",
        "",
        "| candidate_id | frost_score | 棄却理由 | near_dup |",
        "|-------------|-------------|----------|----------|",
    ]
    for d in sorted(rejected, key=lambda x: x.frost_score, reverse=True):
        ev         = eval_by_cid.get(d.candidate_id)
        cid_short  = d.candidate_id[:8]
        fs         = _fmt_float(d.frost_score, 6)
        reason     = d.decision_reason[:80].replace("|", "\\|")
        near_dup   = "✅" if d.suppressed_by_dedup else "—"
        lines.append(f"| `{cid_short}...` | {fs} | {reason} | {near_dup} |")

    lines.append("")
    return "\n".join(lines)
